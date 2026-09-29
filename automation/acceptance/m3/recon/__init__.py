"""The M3 reconnaissance harness (ADR-0010 §4, §5; Issue #52 §3).

It performs the one bounded source reconnaissance that must precede the KM통상 parser and profile
(PR-C). It is dry/fake by default, and a REAL run happens only after the user's explicit go-ahead
through the interactive approval STOP. No reconnaissance output leaves the campaign directory
except the sanitized findings.
"""
