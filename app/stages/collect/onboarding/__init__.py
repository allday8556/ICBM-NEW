"""Self-service supplier onboarding (ADR-0036).

An onboarded supplier is a template-platform site the operator added on screen. Its configuration
is data, not code: append-only database revisions (``models``), written only by
``store.OnboardingStore``, and turned into the same ``SiteConfig`` a repository site file yields,
so the platform templates bind it unchanged (``catalog``). The running process rebinds one
supplier when its record changes; no restart is needed (ADR-0036 §4).

It lives in COLLECT because what it owns is collection knowledge: the words and hosts a template
reads a site with. The COLLECT source-truth boundary therefore applies to it as it is: no AI, OCR
or marketplace import (OB-09).
"""
