import secrets


def new_token() -> str:
    return secrets.token_urlsafe(32)


def bearer_matches(header: str, token: str) -> bool:
    scheme, _, presented = header.partition(" ")
    if scheme.lower() != "bearer" or not presented:
        return False
    return secrets.compare_digest(presented, token)
