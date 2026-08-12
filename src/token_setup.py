"""One-off helper: turn a dashboard/Explorer token into the secrets Phase 4 needs.

    uv run python -m src.token_setup

Handles both login paths, because they need completely different calls:

  Instagram Login   token comes straight from the App Dashboard's "Generate
                    token" button, already long-lived (60 days), refreshable
                    indefinitely. No Facebook Page anywhere.

  Facebook Login    short-lived Explorer token -> long-lived User token ->
                    Page token (no expiry date) -> IG ID via the Page.
                    Order matters: a Page token inherits the lifetime of the
                    User token it came from, so exchanging *first* is what
                    makes it permanent.

Everything is read from a prompt rather than argv, so the app secret never
lands in shell history.
"""

from __future__ import annotations

from getpass import getpass

import requests

from src.config import REPO_ROOT

FB_GRAPH = "https://graph.facebook.com/v26.0"
IG_GRAPH = "https://graph.instagram.com/v26.0"
TIMEOUT = 30


def _get(base: str, path: str, **params) -> dict:
    """GET a Graph endpoint, raising with Meta's own error text on failure.

    Meta puts the useful diagnosis in the response body, not the status line —
    a bare raise_for_status() would throw all of it away.
    """
    resp = requests.get(f"{base}/{path}", params=params, timeout=TIMEOUT)
    payload = resp.json()
    if "error" in payload:
        err = payload["error"]
        raise SystemExit(
            f"\n  Graph API refused {path}:\n"
            f"    {err.get('message')}\n"
            f"    type={err.get('type')} code={err.get('code')} "
            f"subcode={err.get('error_subcode')}\n"
        )
    return payload


def _publishes(base: str, ig_id: str, token: str) -> bool:
    """Does this ID actually accept publishing calls?

    /me returns several plausible-looking identifiers and the docs disagree
    about which one belongs in POST /<IG_ID>/media. Rather than guess, ask the
    publishing surface itself — content_publishing_limit is read-only and only
    resolves for the ID the publishing endpoints accept.
    """
    resp = requests.get(
        f"{base}/{ig_id}/content_publishing_limit",
        params={"access_token": token},
        timeout=TIMEOUT,
    )
    return "error" not in resp.json()


def _instagram_login() -> tuple[str, str, str]:
    """Instagram Login path. Returns (token, ig_id, kind)."""
    print(
        "\n  App Dashboard -> Instagram -> API setup with Instagram business login\n"
        "  -> Generate token, next to your account. Log in and copy the token.\n"
    )
    token = getpass("  Token:         ").strip()

    print("\n[1/2] identifying the account…")
    me = _get(IG_GRAPH, "me", fields="id,username,user_id", access_token=token)
    print(f"      -> @{me.get('username')}")

    print("[2/2] confirming it can publish…")
    # Prefer whichever identifier the publishing surface actually accepts.
    for key in ("user_id", "id"):
        candidate = me.get(key)
        if candidate and _publishes(IG_GRAPH, str(candidate), token):
            print(f"      -> publishing ID resolved from '{key}'")
            return token, str(candidate), "instagram"

    raise SystemExit(
        "\n  The token works but no ID accepted a publishing call.\n"
        "  Usually means instagram_business_content_publish wasn't granted —\n"
        "  re-generate the token and tick it on the consent screen.\n"
    )


def _facebook_login() -> tuple[str, str, str]:
    """Facebook Login path. Returns (token, ig_id, kind)."""
    print("\n  App Dashboard -> Settings -> Basic:")
    app_id = input("  App ID:        ").strip()
    app_secret = getpass("  App Secret:    ").strip()
    print("\n  The token from the Graph API Explorer:")
    short = getpass("  Token:         ").strip()

    print("\n[1/3] exchanging for a long-lived User token…")
    long_lived = _get(
        FB_GRAPH,
        "oauth/access_token",
        grant_type="fb_exchange_token",
        client_id=app_id,
        client_secret=app_secret,
        fb_exchange_token=short,
    )["access_token"]

    print("[2/3] finding your Page…")
    pages = _get(FB_GRAPH, "me/accounts", access_token=long_lived).get("data", [])
    if not pages:
        raise SystemExit(
            "\n  No Pages came back — this path cannot work without one.\n"
            "  Re-run and choose the Instagram Login path instead; it needs\n"
            "  no Facebook Page at all.\n"
        )
    if len(pages) > 1:
        print("\n  Several Pages found:")
        for i, page in enumerate(pages):
            print(f"    [{i}] {page['name']}")
        page = pages[int(input("  Which one is linked to Instagram? ").strip())]
    else:
        page = pages[0]
        print(f"      -> {page['name']}")

    # The Page token has no expiry date, so it is the right default; the User
    # token is offered because it is what you may already have in hand, at the
    # cost of a 60-day refresh that must not be forgotten.
    print("\n  Which token should the publisher use?")
    print(f"    [1] Page token  — no expiry date        (from '{page['name']}')")
    print("    [2] User token  — expires in 60 days")
    use_page = input("  Choose [1]: ").strip() != "2"
    token = page["access_token"] if use_page else long_lived

    print("[3/3] resolving the Instagram account…")
    linked = _get(
        FB_GRAPH, page["id"], fields="instagram_business_account", access_token=token
    ).get("instagram_business_account")
    if not linked:
        raise SystemExit(
            f"\n  Page '{page['name']}' has no Instagram account linked.\n"
            "  Instagram app -> Settings -> Accounts Centre -> add the Page.\n"
        )

    who = _get(FB_GRAPH, linked["id"], fields="username", access_token=token)
    print(f"      -> @{who['username']}")
    return token, linked["id"], "page" if use_page else "user"


def _write_env(**values: str) -> None:
    env = REPO_ROOT / ".env"
    existing = env.read_text().splitlines() if env.is_file() else []
    kept = [ln for ln in existing if ln.split("=")[0].strip() not in values]
    env.write_text("\n".join(kept + [f"{k}={v}" for k, v in values.items()]) + "\n")
    print(f"\n  Wrote {len(values)} secrets to {env.relative_to(REPO_ROOT)} (gitignored).")


def main() -> None:
    print("\n  Which login path?")
    print("    [1] Instagram Login — no Facebook Page, 60-day refreshable token")
    print("    [2] Facebook Login  — needs a Page, can yield a non-expiring token")
    if input("  Choose [1]: ").strip() == "2":
        token, ig_id, kind = _facebook_login()
    else:
        token, ig_id, kind = _instagram_login()

    print(f"\n  Verified: publishing as IG user {ig_id} via a '{kind}' token.\n")
    _write_env(META_ACCESS_TOKEN=token, IG_USER_ID=ig_id, META_TOKEN_KIND=kind)

    print(
        "\n  Add the same values as GitHub repo secrets:\n"
        "    Settings -> Secrets and variables -> Actions\n"
        f"  IG_USER_ID is {ig_id}; the token is in .env — don't paste it into\n"
        "  a chat window.\n"
    )
    if kind != "page":
        print(
            "  This token expires ~60 days from issue. Phase 4 will refresh it\n"
            "  on a schedule and open a GitHub issue if that ever fails.\n"
        )


if __name__ == "__main__":
    main()
