#!/usr/bin/env python3
"""bench-token.py — mint and NUT-07-verify the single-use Cashu tokens the bench spends.

Two subcommands, one file, because the bench workflow is always the pair:

    mint     request an UNSIGNED NUT-04 quote against a FakeWallet test mint, wait for it to
             settle, mint the proofs and write a V3 (`cashuA...`) token to a file (mode 600).
             Refuses to spend anything without --yes.
    verify   NUT-07 `POST /v1/checkstate` on the token's own proofs: every proof must be
             UNSPENT. The bench e2e runs this immediately before a paid phase, because a
             token is single-use and an already-spent one burns a whole bench window.

WHY THIS IS NOT A DUPLICATE OF `scripts/mint-token`
    `scripts/mint-token` is the Go/gonuts-wallet minter: a compiled 12 MB binary with a
    pinned default mint, and it goes through the Go wallet's own quote flow. It stays as it
    is. This tool exists for the case that Go lane could not serve, measured 2026-09-26:

      * the Python `cashu` 0.21 wallet's `request_mint()` ALWAYS generates a NUT-20 keypair
        and sends `pubkey_hex`, so the quote is signature-locked; this mint build rejects
        that signature with `Answer ... "code":20008` /
        `QuoteSignatureInvalidError: Mint quote requires a valid signature`. NUT-20 is
        OPTIONAL — a quote created without a pubkey needs no signature at all.
      * `lib/cashu.py:HttpMinter` already mints over plain HTTP NUT-04 and never sends a
        pubkey, so an unsigned quote is what it produces by construction. This file is a
        CLI over that existing kit code plus the NUT-07 primitives in
        `scripts/recover_tokens.py` — it does NOT reimplement either (the repo's rule: one
        rail library, not copies).

    `scripts/setup-cashu.sh` is the venv bootstrap for the Python `cashu` CLI (used by
    `lib.cashu.CashuMint` and `scripts/recover_tokens.py`); it is what installs the very
    library whose NUT-20 signing gets rejected, so it is not the place to fix this.

USAGE
    scripts/mt3000-bench/bench-token.py mint --amount 64 --out ~/.tg-e2e/tokens/tok1.txt --yes
    scripts/mt3000-bench/bench-token.py verify --token-file ~/.tg-e2e/tokens/tok1.txt

ENV
    MINT_URL      https://testnut.cashu.exchange   (--mint overrides)
    TOKEN_OUT     ~/.tg-e2e/tokens/...             (mint --out overrides)
    MINT_AMOUNT   64                               (mint --amount overrides)

EXIT CODES
    0 ok | 1 the token is not spendable (SPENT/PENDING/UNKNOWN) | 2 usage
    3 the mint could not be reached / answered an error | 4 the minted token failed self-check

The minted token file is written mode 600 and the token itself is never echoed in full —
only its prefix and the output path (it is money, even on a test mint).
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(REPO_ROOT), str(REPO_ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

DEFAULT_MINT = os.environ.get("MINT_URL", "https://testnut.cashu.exchange")
DEFAULT_AMOUNT = int(os.environ.get("MINT_AMOUNT", "64"))
DEFAULT_TOKEN_DIR = Path(os.environ.get("TOKEN_DIR", str(Path.home() / ".tg-e2e" / "tokens")))

EX_OK = 0
EX_NOT_SPENDABLE = 1
EX_USAGE = 2
EX_MINT = 3
EX_SELFCHECK = 4


def _load_recover_tokens():
    """The NUT-07 primitives live in scripts/recover_tokens.py — reuse, never copy."""
    try:
        import recover_tokens  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - depends on the host env
        print(f"bench-token: cannot import scripts/recover_tokens.py: {exc}", file=sys.stderr)
        raise SystemExit(EX_MINT) from exc
    return recover_tokens


def _token_summary(rt, token: str) -> tuple[str, int, int]:
    """Return (mint_url, total_sats, proof_count) for a serialized token."""
    decoded = rt.decode_cashu_token(token)
    proofs = rt.extract_proofs(decoded)
    if not proofs:
        return "", 0, 0
    total = sum(int(p.get("amount", 0)) for p in proofs)
    return rt.extract_mint_url(decoded) or "", total, len(proofs)


def _checkstate(rt, mint_url: str, token: str, timeout: int) -> dict:
    try:
        return rt.check_token_state(mint_url, token, timeout=timeout)
    except Exception as exc:  # requests raises, mints answer 400/500, DNS dies
        print(f"bench-token: NUT-07 checkstate against {mint_url} failed: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        print("bench-token: FAIL CLOSED — an unreadable spend state is not 'unspent'.", file=sys.stderr)
        raise SystemExit(EX_MINT) from exc


def cmd_mint(args: argparse.Namespace) -> int:
    out = Path(os.path.expanduser(args.out)) if args.out else (
        DEFAULT_TOKEN_DIR / f"token-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.txt"
    )
    print(f"bench-token mint: mint={args.mint} amount={args.amount} sat out={out}")
    print("bench-token mint: UNSIGNED quote (no NUT-20 pubkey) -> no 20008 signature rejection")
    if not args.yes:
        print("DRY-RUN: nothing minted. Re-run with --yes to request a real token.")
        return EX_OK

    from lib.cashu import HttpMinter  # noqa: PLC0415

    if not HttpMinter.is_available():
        print("bench-token: HttpMinter unavailable (pip install coincurve)", file=sys.stderr)
        return EX_MINT

    try:
        token = HttpMinter(args.mint).mint(amount=args.amount, legacy=True, timeout=args.timeout)
    except Exception as exc:
        print(f"bench-token: mint failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EX_MINT

    rt = _load_recover_tokens()
    mint_of_token, total, count = _token_summary(rt, token)
    if total != args.amount or count == 0:
        print(f"bench-token: SELF-CHECK FAILED: token carries {total} sat in {count} proofs, "
              f"expected {args.amount}", file=sys.stderr)
        return EX_SELFCHECK

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(token.strip() + "\n")
    os.chmod(out, 0o600)
    print(f"bench-token: minted {total} sat in {count} proofs, mint={mint_of_token or args.mint}")
    print(f"bench-token: token written to {out} (mode 600); prefix={token[:24]}...")

    state = _checkstate(rt, mint_of_token or args.mint, token, args.timeout)
    unspent = state.get("unspent_count", 0)
    print(f"bench-token: NUT-07 immediately after minting: {state}")
    if unspent != state.get("total_proofs", 0) or unspent == 0:
        print("bench-token: minted token does not read back as fully UNSPENT — do not spend it",
              file=sys.stderr)
        return EX_NOT_SPENDABLE
    print("VERDICT=UNSPENT-OK")
    return EX_OK


def cmd_verify(args: argparse.Namespace) -> int:
    path = Path(os.path.expanduser(args.token_file))
    if not path.is_file() or path.stat().st_size == 0:
        print(f"bench-token: no such token file (or empty): {path}", file=sys.stderr)
        return EX_USAGE
    token = path.read_text().strip()
    if not token:
        print(f"bench-token: token file is empty: {path}", file=sys.stderr)
        return EX_USAGE

    rt = _load_recover_tokens()
    mint_of_token, total, count = _token_summary(rt, token)
    mint_url = args.mint or mint_of_token or DEFAULT_MINT
    if not mint_of_token and not args.mint:
        print(f"bench-token: the token carries no mint URL; assuming {DEFAULT_MINT} "
              f"(override with --mint or MINT_URL)")
    print(f"bench-token verify: {path} mint={mint_url} sats={total} proofs={count}")

    if args.expect_amount is not None and total != args.expect_amount:
        print(f"bench-token: amount mismatch: token has {total} sat, expected {args.expect_amount}",
              file=sys.stderr)
        return EX_NOT_SPENDABLE

    state = _checkstate(rt, mint_url, token, args.timeout)
    print(f"bench-token: NUT-07 checkstate: {state}")
    unspent = state.get("unspent_count", 0)
    proof_total = state.get("total_proofs", 0)
    if unspent == 0 or unspent != proof_total:
        print(f"bench-token: NOT SPENDABLE — {unspent}/{proof_total} proofs UNSPENT "
              f"(spent={state.get('spent_count', 0)} unresolved={state.get('unknown_count', 0)})",
              file=sys.stderr)
        print("VERDICT=NOT-USABLE")
        return EX_NOT_SPENDABLE
    if state.get("unknown_count"):
        print(f"bench-token: WARNING: {state['unknown_count']} proof(s) unresolved by the mint; "
              f"some mints answer UNSPENT for Ys they do not know.")
    print("VERDICT=UNSPENT-OK")
    return EX_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bench-token.py",
        description="Mint and NUT-07-verify the single-use Cashu tokens the bench spends.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Exit codes: 0 ok, 1 not spendable, 2 usage, 3 mint unreachable/error, 4 self-check failed.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    mint = sub.add_parser("mint", help="mint one token via an UNSIGNED NUT-04 quote (needs --yes)")
    mint.add_argument("--mint", default=DEFAULT_MINT, help=f"mint URL (default {DEFAULT_MINT})")
    mint.add_argument("--amount", type=int, default=DEFAULT_AMOUNT,
                      help=f"amount in sats (default {DEFAULT_AMOUNT})")
    mint.add_argument("--out", default="", help="output token file (default $TOKEN_DIR/token-<ts>.txt)")
    mint.add_argument("--timeout", type=int, default=60, help="quote settlement timeout in seconds")
    mint.add_argument("--yes", action="store_true", help="actually mint (without it: dry run)")
    mint.set_defaults(func=cmd_mint)

    verify = sub.add_parser("verify", help="NUT-07 checkstate: every proof must be UNSPENT")
    verify.add_argument("--token-file", required=True, help="token file to check")
    verify.add_argument("--mint", default="", help="override the mint URL (default: the token's own)")
    verify.add_argument("--expect-amount", type=int, default=None, help="fail unless the token is this many sats")
    verify.add_argument("--timeout", type=int, default=15, help="checkstate HTTP timeout in seconds")
    verify.set_defaults(func=cmd_verify)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
