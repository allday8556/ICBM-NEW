class RegisterService:
    """REGISTER owner. RegistrationAttempt / MarketplaceRegistration arrive in M5.

    A registration candidate is not a canonical Product. That a Product exists says nothing about
    its pricing readiness, its registration preflight or whether it may be registered at all
    (ADR-0013 §7–§8). Until PR-D and M5 supply that server-derived contract there is no candidate
    source, so the count is 0 rather than the canonical product count (PR #83 review
    5253314334).
    """

    def registration_candidate_count(self) -> int:
        return 0

    def registration_count(self) -> int:
        return 0
