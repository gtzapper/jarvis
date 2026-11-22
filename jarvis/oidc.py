"""Single sign-on with OpenID Connect: Authentik, Keycloak, Authelia, Zitadel, Pocket ID, Google and friends.

Authorization code flow with PKCE, state and nonce. The ID token's signature is checked (RS256/ES256 against the
provider's published keys, or HS256 with the client secret, which Authentik uses when no signing key is set),
plus issuer, audience, expiry and nonce. Groups (e.g. Authentik groups) can decide who is an admin and who is family."""

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
from urllib.parse import urlencode

import requests

from .config import env

_discovery: dict[str, tuple[float, dict]] = {}
_jwks: dict[str, tuple[float, dict]] = {}
_pending: dict[str, dict] = {}  # state -> {verifier, nonce, created, next}
_lock = threading.Lock()


class SSOError(Exception):
    pass


# Popular providers: what to put in Setup, and how each one names things. 'groups' = it can send group membership.
PRESETS = {
    "authentik": {"label": "Authentik", "groups": True, "issuer": "https://auth.example.com/application/o/<app-slug>/",
                  "steps": "Applications > Create with provider > OAuth2/OpenID Provider. Client type Confidential. Redirect URI: the one shown "
                           "below. Copy the 'OpenID Configuration Issuer', client ID and secret. Create the groups jarvis-admins and "
                           "jarvis-family and add people. (The default 'profile' scope already sends groups.)"},
    "keycloak": {"label": "Keycloak", "groups": True, "issuer": "https://sso.example.com/realms/<realm>",
                 "steps": "Clients > Create client (OpenID Connect), Client authentication ON, Standard flow ON. Valid redirect URI: the one "
                          "below. Credentials tab: copy the secret. To send groups: Client scopes > <client>-dedicated > Add mapper > "
                          "Group Membership, token claim name 'groups', Full group path OFF."},
    "authelia": {"label": "Authelia", "groups": True, "issuer": "https://auth.example.com",
                 "steps": "Add a client under identity_providers.oidc.clients: client_id, a hashed client_secret, redirect_uris (the one "
                          "below), scopes [openid, profile, email, groups], authorization_policy two_factor. Restart Authelia."},
    "zitadel": {"label": "Zitadel", "groups": False, "issuer": "https://<instance>.zitadel.cloud",
                "steps": "Projects > New > Application > Web, authentication method CODE (or PKCE). Redirect URI: the one below. Roles "
                         "aren't sent as 'groups', so use the allowed emails below (or let everyone in as family)."},
    "pocketid": {"label": "Pocket ID", "groups": True, "issuer": "https://id.example.com",
                 "steps": "OIDC Clients > Add. Callback URL: the one below. Copy the client ID and secret. Create user groups "
                          "jarvis-admins and jarvis-family and allow them on the client."},
    "kanidm": {"label": "Kanidm", "groups": True, "issuer": "https://idm.example.com/oauth2/openid/<client-name>",
               "steps": "kanidm system oauth2 create jarvis 'Jarvis' https://<jarvis>; add-redirect-url with the URL below; "
                        "update-scope-map <group> openid profile email groups; show-basic-secret for the secret."},
    "google": {"label": "Google", "groups": False, "issuer": "https://accounts.google.com",
               "steps": "console.cloud.google.com > APIs & Services > Credentials > Create OAuth client ID > Web application. Authorized "
                        "redirect URI: the one below (Google needs HTTPS, or http://localhost). Google sends no groups: list who may "
                        "sign in under allowed emails."},
    "microsoft": {"label": "Microsoft", "groups": True, "issuer": "https://login.microsoftonline.com/<tenant-id>/v2.0",
                  "steps": "entra.microsoft.com > App registrations > New registration, Web redirect URI: the one below. Certificates & "
                           "secrets > New client secret. Use your tenant ID in the issuer (not 'common'). For groups: Token configuration "
                           "> Add groups claim; Microsoft sends group IDs, so paste the group's Object ID as the admin/family group."},
    "okta": {"label": "Okta", "groups": True, "issuer": "https://<your-org>.okta.com/oauth2/default",
             "steps": "Applications > Create App Integration > OIDC > Web Application. Sign-in redirect URI: the one below. For groups "
                      "add a 'groups' claim to the authorization server (Security > API > default > Claims)."},
    "other": {"label": "Single sign-on", "groups": True, "issuer": "https://provider.example.com",
              "steps": "Any OpenID Connect provider: create a confidential web client with the redirect URI below. Jarvis reads "
                       "<issuer>/.well-known/openid-configuration."},
}


def _key(s: dict) -> str:
    import re as _re
    return _re.sub(r"[^a-z0-9]+", "-", str(s.get("name") or s.get("provider") or "sso").lower()).strip("-") or "sso"


def providers(cfg: dict) -> list[dict]:
    """The sign-in options that are switched on and filled in, each with a stable '_key' for the login button."""
    raw = cfg.get("sso") or []
    raw = [raw] if isinstance(raw, dict) else raw  # older single-provider config
    out, seen = [], set()
    for s in raw:
        if s.get("enabled", True) and s.get("issuer") and s.get("client_id"):
            preset = PRESETS.get(s.get("provider") or "other", PRESETS["other"])
            k = _key(s)
            while k in seen:  # two entries with the same label
                k += "-2"
            seen.add(k)
            out.append({**s, "_key": k, "_label": s.get("name") or preset["label"]})
    return out


def settings(cfg: dict, key: str | None = None) -> dict | None:
    """One provider (by key), or the first one; None when single sign-on is off."""
    ps = providers(cfg)
    if key:
        return next((p for p in ps if p["_key"] == key), None)
    return ps[0] if ps else None


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def discover(issuer: str, verify: bool = True) -> dict:
    issuer = issuer.rstrip("/")
    hit = _discovery.get(issuer)
    if hit and time.time() - hit[0] < 3600:
        return hit[1]
    try:
        r = requests.get(issuer + "/.well-known/openid-configuration", timeout=10, verify=verify)
        r.raise_for_status()
        d = r.json()
    except (requests.RequestException, ValueError) as e:
        raise SSOError(f"Couldn't read the provider's settings from {issuer}/.well-known/openid-configuration ({e})")
    for k in ("authorization_endpoint", "token_endpoint", "issuer"):
        if not d.get(k):
            raise SSOError(f"The provider's settings are missing {k}")
    _discovery[issuer] = (time.time(), d)
    return d


def start(cfg: dict, redirect_uri: str, next_url: str = "/", key: str | None = None, invite: str | None = None) -> str:
    """The URL to send the browser to. invite = an invitation token, so the new account gets the invited role."""
    s = settings(cfg, key)
    if not s:
        raise SSOError("That sign-in option isn't set up")
    d = discover(s["issuer"], s.get("verify_tls", True))
    verifier = _b64url(secrets.token_bytes(32))
    state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    with _lock:
        for k in [k for k, v in _pending.items() if time.time() - v["created"] > 600]:
            _pending.pop(k, None)
        _pending[state] = {"verifier": verifier, "nonce": nonce, "created": time.time(), "next": next_url, "redirect_uri": redirect_uri,
                           "key": s["_key"], "invite": invite}
    params = {"response_type": "code", "client_id": s["client_id"], "redirect_uri": redirect_uri,
              "scope": s.get("scopes") or "openid profile email", "state": state, "nonce": nonce,
              "code_challenge": _b64url(hashlib.sha256(verifier.encode()).digest()), "code_challenge_method": "S256"}
    return d["authorization_endpoint"] + ("&" if "?" in d["authorization_endpoint"] else "?") + urlencode(params)


def _verify_signature(token: str, d: dict, s: dict, secret: str | None) -> dict:
    head_b64, body_b64, sig_b64 = token.split(".")
    header = json.loads(_unb64(head_b64))
    signed, sig = f"{head_b64}.{body_b64}".encode(), _unb64(sig_b64)
    alg = header.get("alg")
    if alg == "HS256":
        if not secret or not hmac.compare_digest(hmac.new(secret.encode(), signed, hashlib.sha256).digest(), sig):
            raise SSOError("The sign-in token's signature didn't check out")
    elif alg in ("RS256", "RS384", "RS512", "ES256", "ES384", "PS256"):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa, utils
        uri = d.get("jwks_uri")
        if not uri:
            raise SSOError("The provider publishes no signing keys (jwks_uri)")
        hit = _jwks.get(uri)
        keys = hit[1] if hit and time.time() - hit[0] < 3600 else None
        if keys is None or not any(k.get("kid") == header.get("kid") for k in keys.get("keys", [])):
            try:
                keys = requests.get(uri, timeout=10, verify=s.get("verify_tls", True)).json()
            except (requests.RequestException, ValueError) as e:
                raise SSOError(f"Couldn't read the provider's signing keys from {uri} ({type(e).__name__})")
            _jwks[uri] = (time.time(), keys)
        cands = [k for k in keys.get("keys", []) if not header.get("kid") or k.get("kid") == header.get("kid")]
        h = {"256": hashes.SHA256(), "384": hashes.SHA384(), "512": hashes.SHA512()}[alg[2:]]
        ok = False
        for k in cands:
            try:
                if k.get("kty") == "RSA" and alg[0] in "RP":
                    pub = rsa.RSAPublicNumbers(int.from_bytes(_unb64(k["e"]), "big"), int.from_bytes(_unb64(k["n"]), "big")).public_key()
                    pad = padding.PKCS1v15() if alg[0] == "R" else padding.PSS(padding.MGF1(h), padding.PSS.DIGEST_LENGTH)
                    pub.verify(sig, signed, pad, h)
                elif k.get("kty") == "EC" and alg[0] == "E":
                    curve = {"P-256": ec.SECP256R1(), "P-384": ec.SECP384R1()}[k["crv"]]
                    pub = ec.EllipticCurvePublicNumbers(int.from_bytes(_unb64(k["x"]), "big"), int.from_bytes(_unb64(k["y"]), "big"), curve).public_key()
                    n = len(sig) // 2
                    pub.verify(utils.encode_dss_signature(int.from_bytes(sig[:n], "big"), int.from_bytes(sig[n:], "big")), signed, ec.ECDSA(h))
                else:
                    continue
                ok = True
                break
            except Exception:
                continue
        if not ok:
            raise SSOError("The sign-in token's signature didn't check out")
    else:
        raise SSOError(f"Unsupported token signature ({alg})")
    return json.loads(_unb64(body_b64))


def finish(cfg: dict, state: str, code: str) -> tuple[dict, dict, dict]:
    """Swap the code for tokens, check them, and return (claims incl. userinfo, the provider, the pending sign-in)."""
    with _lock:
        p = _pending.pop(state or "", None)
    if not p or time.time() - p["created"] > 600:
        raise SSOError("That sign-in link expired or was already used. Try again.")
    s = settings(cfg, p["key"])
    if not s:
        raise SSOError("That sign-in option was switched off")
    d = discover(s["issuer"], s.get("verify_tls", True))
    secret = env(s.get("client_secret_env") or "OIDC_CLIENT_SECRET")
    data = {"grant_type": "authorization_code", "code": code, "redirect_uri": p["redirect_uri"], "client_id": s["client_id"],
            "code_verifier": p["verifier"]}
    if secret:
        data["client_secret"] = secret
    try:
        r = requests.post(d["token_endpoint"], data=data, timeout=15, verify=s.get("verify_tls", True), headers={"Accept": "application/json"})
        tok = r.json()
    except (requests.RequestException, ValueError) as e:
        raise SSOError(f"Couldn't reach the provider's token endpoint ({e})")
    if not r.ok or "id_token" not in tok:
        raise SSOError(f"The provider refused the sign-in: {tok.get('error_description') or tok.get('error') or r.status_code}")
    claims = _verify_signature(tok["id_token"], d, s, secret)
    aud = claims.get("aud")
    if claims.get("iss", "").rstrip("/") != d["issuer"].rstrip("/"):
        raise SSOError("The sign-in token came from a different issuer")
    if s["client_id"] not in (aud if isinstance(aud, list) else [aud]):
        raise SSOError("The sign-in token was meant for a different app")
    if float(claims.get("exp", 0)) < time.time() - 60:
        raise SSOError("The sign-in token has expired")
    if claims.get("nonce") != p["nonce"]:
        raise SSOError("The sign-in token doesn't match this sign-in")
    if d.get("userinfo_endpoint") and tok.get("access_token"):
        try:
            info = requests.get(d["userinfo_endpoint"], headers={"Authorization": f"Bearer {tok['access_token']}"}, timeout=10,
                                verify=s.get("verify_tls", True)).json()
            if info.get("sub") == claims.get("sub"):
                claims = {**info, **claims, "groups": info.get("groups") or claims.get("groups") or []}
        except (requests.RequestException, ValueError):
            pass
    if s.get("provider") == "google" and claims.get("email") and claims.get("email_verified") is False:
        raise SSOError("Your Google email address isn't verified")
    return claims, s, p


def _emails(v) -> set[str]:
    items = v if isinstance(v, list) else str(v or "").replace(";", ",").split(",")
    return {str(x).strip().lower() for x in items if str(x).strip()}


def decides_roles(s: dict) -> bool:
    """Whether this provider's groups/email lists are the source of truth for roles (re-checked every sign-in)."""
    return bool(s.get("admin_group") or s.get("family_group") or _emails(s.get("admin_emails")) or _emails(s.get("family_emails")))


def role_for(s: dict, claims: dict) -> str | None:
    """admin / family from the groups claim or the email lists, or None if this person isn't allowed in."""
    groups = {str(g).lower() for g in (claims.get("groups") or claims.get("roles") or [])}
    email = str(claims.get("email") or "").lower()
    verified = claims.get("email_verified", True) is not False
    admin_g, family_g = (s.get("admin_group") or "").lower().strip(), (s.get("family_group") or "").lower().strip()
    if (admin_g and admin_g in groups) or (verified and email and email in _emails(s.get("admin_emails"))):
        return "admin"
    fam = _emails(s.get("family_emails"))
    if (family_g and family_g in groups) or (verified and email and (email in fam or any(e.startswith("@") and email.endswith(e) for e in fam))):
        return "family"
    if not decides_roles(s) and s.get("auto_create"):
        return "family"
    return None
