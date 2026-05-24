"""Backfill bcrypt hashes for portal API keys during the plaintext deprecation window."""

from core.api_key_security import api_key_last8, hash_api_key
from core.database import supabase


def rehash_table(table_name: str) -> int:
    rows = (
        supabase.table(table_name)
        .select("id, api_key")
        .not_.is_("api_key", "null")
        .execute()
        .data
    ) or []

    count = 0
    for row in rows:
        raw_key = row.get("api_key")
        if not raw_key:
            continue

        supabase.table(table_name).update(
            {
                "api_key_hash": hash_api_key(raw_key),
                "api_key_last8": api_key_last8(raw_key),
            }
        ).eq("id", row["id"]).execute()
        count += 1

    return count


def main() -> None:
    trader_count = rehash_table("traders")
    ca_count = rehash_table("client_associates")
    advisor_count = rehash_table("advisors")
    print(f"Rehashed {trader_count} trader keys, {ca_count} CA keys, {advisor_count} advisor keys")


if __name__ == "__main__":
    main()
