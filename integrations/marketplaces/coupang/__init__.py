"""Provider-zero Coupang Open API contracts.

No module in this package opens a socket or grants endpoint adoption. C-AUTH-1 only owns the
request-signing, credential/context, error-classification and fake-transport boundaries that later
endpoint slices must use.
"""
