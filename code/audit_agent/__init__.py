"""Ghost Data deletion-audit agent.

The package turns the participant-facing dataset into an evidence graph, links
evidence to the deletion subject, reconstructs every resource's state at the
case's ``audit_at`` cut-off, applies ``deletion_rules.csv`` and emits the
ordered FINDING -> REMEDIATION -> VERIFICATION plan required by the contract.
"""

__version__ = "1.0.0"
