def public_hotel_policies(hotel):
    """Hide inactive and unconfigured Core policy shells from guest displays."""
    policies = dict.fromkeys(("check_in_out", "child", "smoking", "pet", "general_description"))
    for policy in (hotel.core_snapshot or {}).get("hotel_policies", []) or []:
        kind = policy.get("policy_type")
        if kind not in policies or not policy.get("is_active", True):
            continue
        config = policy.get("config") or {}
        description = policy.get("description") or ""
        if not (policy.get("is_configured", False) or bool(config)):
            continue
        if not config and not description.strip():
            continue
        policies[kind] = {
            "title": policy.get("title") or "",
            "description": description,
            "config": config,
        }
    return policies


