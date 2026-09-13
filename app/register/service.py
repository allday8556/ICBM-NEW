from app.products.service import ProductsService


class RegisterService:
    """REGISTER owner. RegistrationAttempt / MarketplaceRegistration arrive in M5.

    Registration candidates derive from canonical products, so they are empty while the
    product catalogue is.
    """

    def __init__(self, products: ProductsService) -> None:
        self._products = products

    def registration_candidate_count(self) -> int:
        return self._products.product_count()

    def registration_count(self) -> int:
        return 0
