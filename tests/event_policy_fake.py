"""The part of GitHub repo_lib.event_policy reads and writes, shared by the
setup and audit fakes: a repository's visibility and its Actions policies.
At the standard by default, so a test about anything else sees it
converged."""

import copy
import json
import re

from repo_lib import event_policy, gh

_REPO_RE = re.compile(r"^repos/([^/]+/[^/]+)$")
_POLICIES_RE = re.compile(r"^repos/([^/]+/[^/]+)/actions/policies$")
_POLICY_ONE_RE = re.compile(r"^repos/([^/]+/[^/]+)/actions/policies/([^/]+)$")


def standard_policy(**changes):
    policy = {"id": 901, **event_policy._create_body()}
    policy.update(changes)
    return policy


class EventPolicyFake:
    def __init__(self):
        self.private = "false"
        self.private_read_fails = None  # gh stderr text, or None
        self.policies = {"901": standard_policy()}
        # Replaces `policies` from the second listing on: someone changing
        # them between the plan and the write.
        self.policies_later = None
        self._listings = 0
        self.list_fails = None  # gh stderr text, or None
        self.write_fails = None  # gh stderr text, or None
        self.writes = []  # (method, endpoint, body)
        self.headers = []  # every -H value an event-policy call sent

    def answer(self, args, endpoint, method, jq):
        """The answer to a read this fake owns, or None when it is not one."""
        if _REPO_RE.match(endpoint or "") and jq == ".private":
            if self.private_read_fails is not None:
                raise gh.GhError(self.private_read_fails)
            return self.private + "\n"
        if _POLICIES_RE.match(endpoint or "") and method is None:
            self._record_headers(args)
            if self.list_fails is not None:
                raise gh.GhError(self.list_fails)
            self._listings += 1
            if self._listings >= 2 and self.policies_later is not None:
                self.policies = self.policies_later
                self.policies_later = None
            match = re.search(r"select\(\.name == (\".*?\")\)", jq or "")
            wanted = json.loads(match.group(1))
            return "".join(f"{pid}\n" for pid, p in self.policies.items() if p["name"] == wanted)
        m = _POLICY_ONE_RE.match(endpoint or "")
        if m and method is None:
            self._record_headers(args)
            if m.group(2) not in self.policies:
                raise gh.GhError("gh: Not Found (HTTP 404)\n")
            return json.dumps(self.policies[m.group(2)])
        return None

    def write(self, args, endpoint, method, input_bytes):
        """Handles a write this fake owns; False when it is not one."""
        if not (_POLICIES_RE.match(endpoint or "") or _POLICY_ONE_RE.match(endpoint or "")):
            return False
        self._record_headers(args)
        body = json.loads(input_bytes)
        self.writes.append((method, endpoint, body))
        if self.write_fails is not None:
            raise gh.GhError(self.write_fails)
        if method == "POST":
            pid = str(902 + len(self.policies))
            self.policies[pid] = {"id": int(pid), **copy.deepcopy(body)}
        else:
            pid = _POLICY_ONE_RE.match(endpoint).group(2)
            self.policies[pid] = {"id": int(pid), **copy.deepcopy(body)}
        return True

    def _record_headers(self, args):
        self.headers += [args[i + 1] for i, a in enumerate(args) if a == "-H"]
