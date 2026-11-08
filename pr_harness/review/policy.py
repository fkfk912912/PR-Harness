"""Evidence requirements shared by model reviewers and their supervisors."""

REVIEW_EVIDENCE_POLICY = """Evidence requirements for publication:
A unified diff is an excerpt. Missing imports or global bindings in the excerpt do not prove
that they are absent from the file. Verify their absence with tools before reporting undefined names.
Unused values, redundant computations and guesses about unfinished intent alone are not defects.
Require a demonstrated wrong result or resource failure for those claims.
A new validation or conversion is not automatically a broken caller contract. Establish the
supported contract before claiming that hypothetical callers require the old behavior.
Direct dangerous operations are different: dynamic code execution, concatenated SQL or shell
commands, embedded credentials, unsafe deserialization and unconstrained file access can establish
a concrete security risk from the visible operation and its input flow. Report that risk with its
actual preconditions. A complete application exploit or an external caller graph is not required;
missing application context does not establish that a dangerous operation is safe.
An optional hardening flag alone requires a demonstrated security requirement before publication.
Apply these requirements to both scanner and model candidates. Preserve the JSON schema for the
current role and phase. Review workers may return an empty findings list; supervisors must use
their required delegations, decisions or accepted-index schema."""
