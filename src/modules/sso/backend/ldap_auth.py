# ldap_auth.py (module "sso")
# --------------------------
# Directory login against LDAP / Active Directory (formerly app/auth.py).
# The core calls these functions via modules.optional("sso", "ldap_auth"):
#   authenticate_realm(username, password, realm_id) -> user dict | None
#   test_realm_connection(realm) -> dict
#   list_realm_groups(realm) -> list[str]
#
# The ldap3 package is installed by the module management on activation (module.json -> pip).

from fastapi import HTTPException

from app import db


def _import_ldap3():
    """Import ldap3 or return a clear error message if it is missing."""
    try:
        import ldap3
        from ldap3.utils.conv import escape_filter_chars
        return ldap3, escape_filter_chars
    except ImportError:
        raise HTTPException(
            501,
            "AD login not available: the package 'ldap3' is not installed. "
            "Please run 'pip install ldap3' on the server.",
        )


def _build_ldap_server(realm: dict):
    """Baut ein ldap3.Server-Objekt aus der Realm-Konfiguration (inkl. Port/SSL)."""
    ldap3, _ = _import_ldap3()
    use_ssl = bool(realm.get("use_ssl"))
    port = realm.get("port")
    try:
        port = int(port) if port not in (None, "") else None
    except (TypeError, ValueError):
        port = None

    host = realm["server"]
    # If someone enters a full URL (ldaps://host), we respect that.
    if host.startswith("ldaps://"):
        use_ssl = True
        host = host[len("ldaps://"):]
    elif host.startswith("ldap://"):
        host = host[len("ldap://"):]

    return ldap3.Server(
        host,
        port=port,
        use_ssl=use_ssl,
        get_info=ldap3.ALL,
        connect_timeout=8,
    )


def _extract_group_names(member_of: list[str]) -> list[str]:
    """Wandelt AD-memberOf-DNs in reine Gruppennamen um (CN=Gruppe,... -> Gruppe)."""
    names = []
    for dn in member_of:
        first = dn.split(",")[0].strip()
        if first.upper().startswith("CN="):
            names.append(first[3:])
    return names


def test_realm_connection(realm: dict) -> dict:
    """
    Check the realm configuration without logging a user in: connects with the
    bind/service account and does a small search in the base DN.
    Returns a result dict (used by the test button in the frontend).
    """
    ldap3, _ = _import_ldap3()
    server = _build_ldap_server(realm)
    try:
        conn = ldap3.Connection(
            server,
            user=realm.get("bind_user") or None,
            password=realm.get("bind_password") or None,
            auto_bind=True,
        )
    except Exception as e:
        raise HTTPException(502, f"Verbindung/Anmeldung fehlgeschlagen: {e}")

    base_dn = realm.get("base_dn") or ""
    try:
        conn.search(base_dn, "(objectClass=*)", search_scope=ldap3.BASE, attributes=["distinguishedName"])
        info = {
            "ok": True,
            "server": str(server.host),
            "ssl": bool(realm.get("use_ssl")),
            "base_dn_ok": bool(base_dn) and len(conn.entries) >= 0,
        }
    except Exception as e:
        conn.unbind()
        raise HTTPException(502, f"Base DN nicht durchsuchbar: {e}")
    conn.unbind()
    return info


def list_realm_groups(realm: dict) -> list[str]:
    """
    Listet die Gruppennamen (CN) eines AD/LDAP-Verzeichnisses auf. Wird benutzt,
    um AD-Gruppen ins RMM zu importieren, damit man ihnen VORAB Rechte geben kann
    (ohne dass sich erst ein Mitglied anmelden muss).
    """
    ldap3, _ = _import_ldap3()
    server = _build_ldap_server(realm)
    base_dn = realm.get("base_dn") or ""
    try:
        conn = ldap3.Connection(
            server,
            user=realm.get("bind_user") or None,
            password=realm.get("bind_password") or None,
            auto_bind=True,
        )
    except Exception as e:
        raise HTTPException(502, f"AD-Verbindung fehlgeschlagen: {e}")
    try:
        names: set[str] = set()
        # Search paginated so even large directories are loaded completely.
        conn.search(
            base_dn, "(objectClass=group)", search_scope=ldap3.SUBTREE,
            attributes=["cn"], paged_size=500,
        )
        for e in conn.entries:
            if "cn" in e and str(e.cn):
                names.add(str(e.cn))
        # Weitere Seiten holen, falls vorhanden.
        while True:
            cookie = (conn.result.get("controls", {})
                      .get("1.2.840.113556.1.4.319", {})
                      .get("value", {}).get("cookie"))
            if not cookie:
                break
            conn.search(
                base_dn, "(objectClass=group)", search_scope=ldap3.SUBTREE,
                attributes=["cn"], paged_size=500, paged_cookie=cookie,
            )
            for e in conn.entries:
                if "cn" in e and str(e.cn):
                    names.add(str(e.cn))
    except Exception as e:
        conn.unbind()
        raise HTTPException(502, f"Gruppensuche fehlgeschlagen: {e}")
    conn.unbind()
    return sorted(names)


def authenticate_realm(username: str, password: str, realm_id: str) -> dict | None:
    """
    Authenticate a user against a configured directory (LDAP/AD).

    Flow:
      1. Load the realm configuration (server, port, SSL, base DN, bind user).
      2. Bind to LDAP with the bind user and search for the user
         (sAMAccountName OR userPrincipalName, filter characters are escaped).
      3. Bind a second time with the found DN + the entered password
         - if that works, the password is correct.
      4. Read the user's AD groups (incl. nested groups, as far as the
         directory supports it).
      5. Create/update the user locally (without a password hash) and map them
         to the RMM groups of the same name.

    Returns the local user dict or None on failure.
    """
    ldap3, escape_filter_chars = _import_ldap3()

    realm = db.get_realm(realm_id)
    if not realm:
        return None
    if not realm.get("enabled", 1):
        raise HTTPException(403, "Dieses Verzeichnis ist deaktiviert.")

    base_dn = realm.get("base_dn") or ""
    bind_user = realm.get("bind_user")
    bind_password = realm.get("bind_password")
    extra_filter = (realm.get("user_filter") or "").strip()

    safe_username = escape_filter_chars(username)
    user_filter = f"(|(sAMAccountName={safe_username})(userPrincipalName={safe_username}))"
    search_filter = f"(&(objectClass=user){user_filter}{extra_filter})"

    server = _build_ldap_server(realm)
    try:
        # 1. Mit dem Service-/Bind-Account anmelden, um den Benutzer zu finden
        conn = ldap3.Connection(
            server, user=bind_user or None, password=bind_password or None, auto_bind=True
        )

        conn.search(
            base_dn, search_filter, search_scope=ldap3.SUBTREE,
            attributes=["memberOf", "displayName", "sAMAccountName"],
        )
        if not conn.entries:
            conn.unbind()
            return None

        entry = conn.entries[0]
        user_dn = entry.entry_dn
        display_name = str(entry.displayName) if "displayName" in entry else username
        member_of = [str(g) for g in entry.memberOf] if "memberOf" in entry else []

        # 4b. Verschachtelte Gruppen (AD): Gruppen, in denen der User indirekt
        #     is a member. If that fails (e.g. non-AD LDAP), we ignore it.
        try:
            nested_filter = f"(member:1.2.840.113556.1.4.1943:={escape_filter_chars(user_dn)})"
            conn.search(base_dn, nested_filter, search_scope=ldap3.SUBTREE, attributes=["cn"])
            for g in conn.entries:
                member_of.append(g.entry_dn)
        except Exception:
            pass
        conn.unbind()

        # 2. Bind with the found DN + the entered password (= password check)
        if not password:
            return None  # never wave an empty password through as "anonymously valid"
        user_conn = ldap3.Connection(server, user=user_dn, password=password)
        if not user_conn.bind():
            return None  # falsches Passwort
        user_conn.unbind()

    except HTTPException:
        raise
    except Exception as e:
        # Netzwerkfehler, falscher Bind-User etc.
        raise HTTPException(502, f"AD-Anmeldung fehlgeschlagen: {e}")

    # 3. AD-Gruppennamen aus den memberOf-DNs extrahieren (dedupliziert)
    ad_group_names = sorted(set(_extract_group_names(member_of)))

    # 5. Benutzer lokal anlegen/aktualisieren und AD-Gruppen zuordnen
    user = db.upsert_ad_user(username, display_name, realm_id)
    db.sync_ad_user_groups(user["id"], ad_group_names)
    return db.get_user_by_id(user["id"])
