"""
generate_session.py — Run this LOCALLY on your PC (not on Render) to generate a session string.
Then paste the session string into bot.py or provide it via Supabase.

Usage:
    python generate_session.py
"""
import asyncio
from pyrogram import Client


async def main():
    print("=" * 55)
    print("  Telegram Session String Generator")
    print("=" * 55)
    print("Run this script ONCE on your local PC to generate")
    print("a session string. Paste the result into your bot.\n")

    api_id = input("Enter your API ID: ").strip()
    api_hash = input("Enter your API Hash: ").strip()
    phone = input("Enter your phone number (e.g. +917XXXXXXXXX): ").strip()

    async with Client(
        name="session_gen",
        api_id=int(api_id),
        api_hash=api_hash,
        phone_number=phone,
        in_memory=True,
    ) as client:
        session_string = await client.export_session_string()
        print("\n" + "=" * 55)
        print("✅ SUCCESS! Here is your session string:")
        print("=" * 55)
        print(session_string)
        print("=" * 55)
        print("\nCopy the string above and run this command in your")
        print("terminal to POST it to the bot (or use Supabase UI):")
        print(f"\nSession String (copy carefully, it's one long line):\n")
        print(session_string)


if __name__ == "__main__":
    asyncio.run(main())
