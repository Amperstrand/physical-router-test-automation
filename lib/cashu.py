from __future__ import annotations

import hashlib
import json
import logging
import secrets as _secrets_mod
import shutil
import signal
import socket
import subprocess
import tempfile
import os
import base64
import time
import re
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import TYPE_CHECKING

from urllib import error, request
from urllib.parse import urlparse

from lib.constants import TEST_MINT_URL

if TYPE_CHECKING:
    import coincurve

log = logging.getLogger("tollgate.cashu")


class MintUnavailableError(Exception):
    pass


class _MintTimeoutError(Exception):
    pass


def mint_reachable(mint_url: str, timeout: float = 3.0) -> bool:
    """TCP-probe a mint URL. The cashu CLI can block for minutes against an
    absent mint, so tests should skip via this probe before driving the CLI."""
    try:
        parsed = urlparse(mint_url)
        host = parsed.hostname or mint_url
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


class CashuMint:
    def __init__(self, venv_path: str | None = None, mint_url: str = TEST_MINT_URL):
        venv_path = venv_path or os.environ.get("TOLLGATE_CASHU_VENV", "/opt/cashu-venv")
        self.venv_path = venv_path
        self.mint_url = mint_url
        self._python = os.path.join(venv_path, "bin", "python")
        self._cashu = os.path.join(venv_path, "bin", "cashu")

    def is_available(self) -> bool:
        return os.path.isfile(self._cashu)

    def _env(self):
        env = os.environ.copy()
        env["VIRTUAL_ENV"] = self.venv_path
        env["PATH"] = f"{self.venv_path}/bin:{env.get('PATH', '')}"
        return env

    def ensure_mint_available(self, timeout: int = 15):
        keys_url = f"{self.mint_url.rstrip('/')}/v1/keys"
        req = request.Request(keys_url, headers={"User-Agent": "tollgate-test/1.0"})
        try:
            with request.urlopen(req, timeout=timeout) as response:
                if response.status != 200:
                    raise MintUnavailableError(
                        f"Mint health check failed with HTTP {response.status}"
                    )
        except MintUnavailableError:
            raise
        except (error.URLError, TimeoutError) as exc:
            raise MintUnavailableError(f"cashu mint unavailable: {exc}") from exc
        except Exception as exc:
            raise MintUnavailableError(f"cashu mint unexpected error: {exc}") from exc

    def _find_latest_quote_id(self, url=None):
        mint_url = url or self.mint_url
        r = subprocess.run(
            [self._cashu, "-h", mint_url, "-t", "invoices"],
            capture_output=True, text=True, timeout=30, env=self._env(),
        )
        entries = self._parse_invoices(r.stdout)
        if not entries:
            return None
        return entries[-1].get("id", "").strip()

    @staticmethod
    def _parse_invoices(output):
        entries = []
        current = {}
        for line in output.split("\n"):
            line = line.strip()
            if line.startswith("#"):
                if current:
                    entries.append(current)
                current = {}
            elif ":" in line and current is not None:
                k, v = line.split(":", 1)
                current[k.strip().lower()] = v.strip()
        if current:
            entries.append(current)
        return entries

    def _count_invoices(self):
        r = subprocess.run(
            [self._cashu, "-h", self.mint_url, "-t", "invoices"],
            capture_output=True, text=True, timeout=30, env=self._env(),
        )
        return r.stdout.count("Mint quote")

    def _wait_and_claim(self, quote_id, amount, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(2, remaining))
            try:
                r = subprocess.run(
                    [self._cashu, "-h", self.mint_url, "-t",
                     "invoice", str(amount), "--id", quote_id],
                    capture_output=True, text=True,
                    timeout=min(15, max(5, remaining)),
                    env=self._env(),
                )
                if "Invoice paid" in r.stdout:
                    return True
            except subprocess.TimeoutExpired:
                if time.monotonic() >= deadline:
                    raise MintUnavailableError(
                        f"cashu claim timed out after {timeout}s"
                    )
            except Exception:
                pass
        return False

    def _ensure_balance(self, amount):
        r = subprocess.run(
            [self._cashu, "-h", self.mint_url, "-t", "balance"],
            capture_output=True, text=True, timeout=30, env=self._env(),
        )
        match = re.search(r"Balance:\s*(\d+)", r.stdout)
        if match and int(match.group(1)) >= amount:
            return

        before = self._count_invoices()

        proc = subprocess.Popen(
            [self._cashu, "-h", self.mint_url, "-t", "-y", "invoice", str(amount)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self._env(),
        )
        time.sleep(3)
        proc.kill()
        proc.communicate()

        created = False
        for attempt in range(10):
            after = self._count_invoices()
            if after > before:
                created = True
                break
            time.sleep(2)

        if not created:
            raise RuntimeError(
                f"Invoice not created after 25s (before={before}, after={after}, mint={self.mint_url})"
            )

        quote_id = self._find_latest_quote_id()
        if not quote_id:
            raise RuntimeError("No quote found after invoice creation")

        if not self._wait_and_claim(quote_id, amount, timeout=60):
            raise RuntimeError(f"Mint claim failed for quote {quote_id}")

    def _timeout_handler(self, signum, frame):
        raise _MintTimeoutError()

    def warmup(self, timeout: int = 60) -> None:
        """Pre-initialize the cashu wallet DB and fetch keysets.

        The first ``cashu`` CLI call is slow (Python startup + wallet DB
        creation + keyset fetch ≈ 10-15 s on a cold GCP VM).  Calling this
        once during fixture setup prevents the first real ``mint()`` from
        blowing past the SIGALRM deadline.
        """
        if not self.is_available():
            return
        try:
            subprocess.run(
                [self._cashu, "-h", self.mint_url, "-t", "balance"],
                capture_output=True, text=True, timeout=timeout, env=self._env(),
            )
        except (subprocess.TimeoutExpired, Exception):
            pass  # non-fatal — the real mint() will retry

    def mint(self, amount: int = 4, legacy: bool = True, timeout: int = 120, retries: int = 2) -> str:
        if not self.is_available():
            raise RuntimeError(f"cashu venv not found at {self.venv_path}")

        old_handler = signal.signal(signal.SIGALRM, self._timeout_handler)
        last_err: RuntimeError = RuntimeError("mint failed with no specific error")
        for attempt in range(1 + retries):
            signal.alarm(timeout)
            try:
                return self._mint_inner(amount, legacy)
            except _MintTimeoutError:
                raise MintUnavailableError(
                    f"mint() timed out after {timeout}s"
                )
            except RuntimeError as exc:
                last_err = exc
                if attempt < retries:
                    time.sleep(5 * (attempt + 1))
            finally:
                signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)
        raise last_err  # type: ignore[misc]

    def _mint_inner(self, amount: int, legacy: bool = True) -> str:
        env = self._env()

        self.ensure_mint_available()
        self._ensure_balance(amount)

        cmd = [self._cashu, "-h", self.mint_url, "-t", "-y", "send", str(amount)]
        if legacy:
            cmd.append("--legacy")

        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120, env=env)
        token = r.stdout.strip().split("\n")[0]
        if not token.startswith(("cashuA", "cashuB")):
            raise RuntimeError(f"Send failed: {r.stdout[-200:]}")
        return token

    def mint_from_wrong_mint(self, amount: int = 4, timeout: int = 90) -> str:
        return self.synthetic_wrong_mint_token()

    @staticmethod
    def synthetic_wrong_mint_token() -> str:
        payload = [{"mint": "https://wrong-mint.example.com",
                     "proofs": [{"amount": 4, "secret": "fake", "C": "fake"}]}]
        return "cashuA" + base64.b64encode(json.dumps(payload).encode()).decode()


_CDK_CLI_PATHS = [
    "/opt/cdk-mintd/cdk-cli",
    "/usr/local/bin/cdk-cli",
]


class CdkCliWallet:
    def __init__(self, mint_url: str, cdk_cli_path: str | None = None):
        self.mint_url = mint_url
        if cdk_cli_path:
            self._cli = cdk_cli_path
        else:
            self._cli = self._find_cli()
        self._work_dir: str | None = None

    def _find_cli(self) -> str:
        for path in _CDK_CLI_PATHS:
            if os.path.isfile(path) and os.access(path, os.X_OK):
                return path
        for dir_entry in os.environ.get("PATH", "").split(os.pathsep):
            candidate = os.path.join(dir_entry, "cdk-cli")
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
        return _CDK_CLI_PATHS[0]

    def is_available(self) -> bool:
        return os.path.isfile(self._cli) and os.access(self._cli, os.X_OK)

    def ensure_mint_available(self, timeout: int = 15):
        keys_url = f"{self.mint_url.rstrip('/')}/v1/keys"
        req = request.Request(keys_url, headers={"User-Agent": "tollgate-test/1.0"})
        try:
            with request.urlopen(req, timeout=timeout) as response:
                if response.status != 200:
                    raise MintUnavailableError(
                        f"Mint health check failed with HTTP {response.status}"
                    )
        except MintUnavailableError:
            raise
        except (error.URLError, TimeoutError) as exc:
            raise MintUnavailableError(f"cashu mint unavailable: {exc}") from exc
        except Exception as exc:
            raise MintUnavailableError(f"cashu mint unexpected error: {exc}") from exc

    def _get_work_dir(self) -> str:
        if self._work_dir and os.path.isdir(self._work_dir):
            return self._work_dir
        self._work_dir = tempfile.mkdtemp(prefix="cdk-cli-session-")
        return self._work_dir

    def _mint_token(self, amount: int, timeout: int) -> str:
        work_dir = self._get_work_dir()
        mint_r = subprocess.run(
            [self._cli, "-w", work_dir, "mint", self.mint_url, str(amount)],
            capture_output=True, text=True, timeout=timeout,
        )
        if mint_r.returncode != 0:
            err = mint_r.stderr[-300:]
            if "already exists" in err or "pending" in err.lower():
                shutil.rmtree(work_dir, ignore_errors=True)
                self._work_dir = None
                work_dir = self._get_work_dir()
                mint_r = subprocess.run(
                    [self._cli, "-w", work_dir, "mint", self.mint_url, str(amount)],
                    capture_output=True, text=True, timeout=timeout,
                )
            if mint_r.returncode != 0:
                raise RuntimeError(
                    f"cdk-cli mint failed (exit {mint_r.returncode}): "
                    f"{mint_r.stderr[-300:]}"
                )

        send_r = subprocess.run(
            [self._cli, "-w", work_dir, "send", "--mint-url", self.mint_url, "--v3"],
            input=f"{amount}\n",
            capture_output=True, text=True, timeout=timeout,
        )
        if send_r.returncode != 0:
            raise RuntimeError(
                f"cdk-cli send failed (exit {send_r.returncode}): "
                f"stdout={send_r.stdout[-200:]} stderr={send_r.stderr[-200:]}"
            )

        for line in send_r.stdout.strip().splitlines():
            line = line.strip()
            if line.startswith(("cashuA", "cashuB")):
                return line

        raise RuntimeError(
            f"cdk-cli send produced no token: {send_r.stdout[-300:]}"
        )

    def warmup(self, timeout: int = 60) -> None:
        if not self.is_available():
            return
        work_dir = self._get_work_dir()
        try:
            subprocess.run(
                [self._cli, "-w", work_dir, "balance"],
                capture_output=True, text=True, timeout=timeout,
            )
        except (subprocess.TimeoutExpired, Exception):
            pass  # non-fatal

    def mint(self, amount: int = 4, legacy: bool = False, timeout: int = 90,
             retries: int = 2) -> str:
        if not self.is_available():
            raise RuntimeError(f"cdk-cli not found at {self._cli}")

        last_err: RuntimeError = RuntimeError("mint failed with no specific error")
        for attempt in range(1 + retries):
            try:
                self.ensure_mint_available()
                return self._mint_token(amount, timeout)
            except subprocess.TimeoutExpired:
                raise MintUnavailableError(
                    f"cdk-cli mint() timed out after {timeout}s"
                )
            except RuntimeError as exc:
                last_err = exc
                if attempt < retries:
                    time.sleep(5 * (attempt + 1))
        raise last_err

    @staticmethod
    def synthetic_wrong_mint_token() -> str:
        return CashuMint.synthetic_wrong_mint_token()

    def mint_from_wrong_mint(self, amount: int = 4, timeout: int = 90) -> str:
        return self.synthetic_wrong_mint_token()


class HttpMinter:
    """Direct HTTP Cashu token minter — NUT-04 + BDHKE via coincurve.

    No subprocess overhead. Uses pure HTTP requests + secp256k1 crypto
    to mint V3 tokens directly from any Cashu mint. ~100x faster than
    CLI-based minters under nested KVM (no process spawn overhead).

    Requires ``coincurve`` (already in requirements.txt).
    """

    _DOMAIN_SEPARATOR = b"Secp256k1_HashToCurve_Cashu_"
    # secp256k1 curve order
    _N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141

    def __init__(self, mint_url: str):
        self.mint_url = mint_url.rstrip("/")
        self._keyset_cache: tuple[str, dict[int, str]] | None = None

    # -- availability / health --

    @staticmethod
    def is_available() -> bool:
        try:
            import coincurve  # noqa: F401
            return True
        except ImportError:
            return False

    def ensure_mint_available(self, timeout: int = 15):
        self._http_get(f"{self.mint_url}/v1/keys", timeout=timeout)

    def warmup(self, timeout: int = 60) -> None:
        """Pre-fetch keysets so the first mint() is fast."""
        try:
            self._get_active_keyset()
        except Exception:
            pass  # non-fatal

    # -- public minting API --

    def mint(self, amount: int = 4, legacy: bool = True, timeout: int = 30,
             retries: int = 2) -> str:
        """Mint tokens via direct HTTP NUT-04 flow.

        Steps:
        1. Fetch active keyset
        2. Create mint quote (POST /v1/mint/quote/bolt11)
        3. Wait for payment (FakeWallet auto-pays)
        4. Mint tokens (POST /v1/mint/bolt11) with blinded messages
        5. Unblind signatures
        6. Serialize to V3 token

        Returns: ``cashuA...`` token string.
        """
        import coincurve

        last_err: Exception = RuntimeError("mint failed")
        for attempt in range(1 + retries):
            try:
                return self._mint_inner(amount, timeout)
            except Exception as exc:
                last_err = exc
                if attempt < retries:
                    time.sleep(2 * (attempt + 1))
        raise last_err

    def mint_from_wrong_mint(self, amount: int = 4, timeout: int = 90) -> str:
        return CashuMint.synthetic_wrong_mint_token()

    @staticmethod
    def synthetic_wrong_mint_token() -> str:
        return CashuMint.synthetic_wrong_mint_token()

    # -- internal --

    def _mint_inner(self, amount: int, timeout: int) -> str:
        import coincurve

        # 1. Get active keyset
        keyset_id, keys = self._get_active_keyset()

        # 2. Decompose amount into powers of 2 and generate blinded messages
        powers = self._amount_to_powers(amount)
        outputs: list[dict] = []
        blind_data: list[tuple[str, bytes]] = []  # (secret_hex, blinding_factor)

        for pwr in powers:
            secret_hex, r, b_hex = self._blind_message(pwr)
            outputs.append({"amount": pwr, "id": keyset_id, "B_": b_hex})
            blind_data.append((secret_hex, r))

        # 3. Create mint quote
        quote_data = self._http_post(
            f"{self.mint_url}/v1/mint/quote/bolt11",
            {"unit": "sat", "amount": amount},
        )
        quote_id = quote_data["quote"]

        # 4. Wait for payment (FakeWallet auto-pays immediately)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = self._http_get(
                f"{self.mint_url}/v1/mint/quote/bolt11/{quote_id}"
            )
            if state.get("state") == "PAID":
                break
            time.sleep(0.5)
        else:
            raise MintUnavailableError(
                f"Mint quote {quote_id} not paid after {timeout}s"
            )

        # 5. Mint: POST /v1/mint/bolt11
        mint_data = self._http_post(
            f"{self.mint_url}/v1/mint/bolt11",
            {"quote": quote_id, "outputs": outputs},
        )
        signatures = mint_data["signatures"]

        # 6. Unblind signatures and build proofs
        proofs = []
        for i, sig in enumerate(signatures):
            c_prime = coincurve.PublicKey(bytes.fromhex(sig["C_"]))
            r = blind_data[i][1]
            k_pub = coincurve.PublicKey(bytes.fromhex(keys[sig["amount"]]))

            # C = C_ - r*K  (unblinding)
            rK = k_pub.multiply(r)
            neg_rK = self._negate_point(rK)
            c = coincurve.PublicKey.combine_keys([c_prime, neg_rK])

            proofs.append({
                "amount": sig["amount"],
                "id": sig["id"],
                "secret": blind_data[i][0],
                "C": c.format().hex(),
            })

        # 7. Serialize to V3 token
        return self._serialize_v3(proofs)

    # -- HTTP helpers --

    def _http_get(self, url: str, timeout: int = 15) -> dict:
        req = request.Request(url, headers={"User-Agent": "tollgate-test/1.0"})
        try:
            with request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read())
        except error.HTTPError as exc:
            body = exc.read().decode()[:200]
            raise MintUnavailableError(f"HTTP {exc.code}: {body}") from exc
        except (error.URLError, TimeoutError) as exc:
            raise MintUnavailableError(f"GET {url} failed: {exc}") from exc

    def _http_post(self, url: str, body: dict, timeout: int = 15) -> dict:
        data = json.dumps(body).encode()
        req = request.Request(
            url, data=data,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "tollgate-test/1.0",
            },
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read())
        except error.HTTPError as exc:
            body_text = exc.read().decode()[:300]
            raise MintUnavailableError(
                f"POST {url} HTTP {exc.code}: {body_text}"
            ) from exc
        except (error.URLError, TimeoutError) as exc:
            raise MintUnavailableError(f"POST {url} failed: {exc}") from exc

    # -- crypto helpers --

    def _get_active_keyset(self, unit: str = "sat") -> tuple[str, dict[int, str]]:
        """Fetch and cache the active keyset for the given unit."""
        if self._keyset_cache is not None:
            return self._keyset_cache

        data = self._http_get(f"{self.mint_url}/v1/keys")
        for ks in data.get("keysets", []):
            if ks.get("unit") == unit and ks.get("active", True):
                keys = {int(k): v for k, v in ks.get("keys", {}).items()}
                result = (ks["id"], keys)
                self._keyset_cache = result
                return result

        raise RuntimeError(f"No active {unit} keyset at {self.mint_url}")

    @classmethod
    def _hash_to_curve(cls, message: bytes) -> coincurve.PublicKey:
        """Deterministic map from message bytes to secp256k1 point (NUT-00)."""
        import coincurve

        msg_hash = hashlib.sha256(cls._DOMAIN_SEPARATOR + message).digest()
        counter = 0
        while counter < 2**32:
            counter_bytes = counter.to_bytes(4, "little")
            candidate = hashlib.sha256(msg_hash + counter_bytes).digest()
            try:
                return coincurve.PublicKey(b"\x02" + candidate)
            except Exception:
                counter += 1
        raise RuntimeError("hash_to_curve: no valid point found")

    @staticmethod
    def _blind_message(amount: int) -> tuple[str, bytes, str]:
        """Generate a blinded message for one output.

        Returns (secret_hex, blinding_factor_32bytes, B__compressed_hex).
        """
        import coincurve

        secret_hex = _secrets_mod.token_hex(32)
        y = HttpMinter._hash_to_curve(secret_hex.encode("utf-8"))
        r = _secrets_mod.token_bytes(32)

        # B_ = Y + r*G
        b_ = y.add(r)

        return secret_hex, r, b_.format().hex()

    @staticmethod
    def _negate_point(pk: coincurve.PublicKey) -> coincurve.PublicKey:
        """Negate a secp256k1 point (flip 02↔03 prefix)."""
        import coincurve

        raw = pk.format()
        prefix = b"\x03" if raw[0] == 2 else b"\x02"
        return coincurve.PublicKey(prefix + raw[1:])

    @staticmethod
    def _amount_to_powers(amount: int) -> list[int]:
        """Decompose amount into powers of 2 (Cashu denomination scheme)."""
        powers = []
        power = 1
        while amount > 0:
            if amount & 1:
                powers.append(power)
            amount >>= 1
            power <<= 1
        return powers

    def _serialize_v3(self, proofs: list[dict]) -> str:
        """Serialize proofs to a V3 Cashu token (cashuA...)."""
        token_obj = {
            "token": [{"mint": self.mint_url, "proofs": proofs}],
            "unit": "sat",
        }
        token_json = json.dumps(token_obj, separators=(",", ":"))
        token_b64 = base64.urlsafe_b64encode(
            token_json.encode()
        ).decode().rstrip("=")
        return "cashuA" + token_b64


def create_minter(
    mint_url: str = TEST_MINT_URL,
    venv_path: str | None = None,
) -> CashuMint | CdkCliWallet | HttpMinter:
    # Prefer HttpMinter (pure HTTP, no subprocess) when coincurve is available
    if HttpMinter.is_available():
        try:
            minter = HttpMinter(mint_url)
            minter.ensure_mint_available(timeout=5)
            log.info("create_minter: selected HttpMinter (mint=%s)", mint_url)
            return minter
        except Exception as exc:
            log.warning(
                "create_minter: HttpMinter unavailable (mint=%s, error=%s); falling back",
                mint_url, exc,
            )
    else:
        log.info("create_minter: coincurve not installed; skipping HttpMinter")

    cdk = CdkCliWallet(mint_url)
    if cdk.is_available():
        log.info("create_minter: selected CdkCliWallet (mint=%s)", mint_url)
        return cdk

    log.info("create_minter: selected CashuMint (mint=%s, venv=%s)", mint_url, venv_path)
    return CashuMint(venv_path=venv_path, mint_url=mint_url)


log = logging.getLogger("tollgate.token_pool")


class TokenPool:
    _POOL_AMOUNT = 4

    def __init__(self, minter: CashuMint | CdkCliWallet | HttpMinter, pool_size: int = 10):
        self._minter = minter
        self._pool_size = pool_size
        self._queue: deque[str] = deque()
        self._lock = threading.Lock()
        self._minter_url = getattr(minter, "mint_url", "<unknown>")
        self._prefill()

    def _prefill(self):
        t0 = time.monotonic()
        successes = 0
        with ThreadPoolExecutor(max_workers=min(self._pool_size, 5)) as pool:
            futures = {
                pool.submit(self._minter.mint, self._POOL_AMOUNT): i
                for i in range(self._pool_size)
            }
            for future in as_completed(futures):
                try:
                    token = future.result()
                    with self._lock:
                        self._queue.append(token)
                    successes += 1
                except Exception as exc:
                    log.warning("TokenPool: prefill mint failed: %s", exc)
        elapsed = time.monotonic() - t0
        log.info(
            "TokenPool: prefilled %d/%d tokens (%.1fs, mint=%s)",
            successes, self._pool_size, elapsed, self._minter_url,
        )

    def _replenish(self):
        try:
            token = self._minter.mint(self._POOL_AMOUNT)
            with self._lock:
                self._queue.append(token)
        except Exception as exc:
            log.warning("TokenPool: replenish failed: %s", exc)

    # -- public API (same interface as CashuMint / CdkCliWallet) --

    def mint(self, amount: int = 4, legacy: bool = True, timeout: int = 120, retries: int = 2) -> str:
        if amount == self._POOL_AMOUNT:
            with self._lock:
                if self._queue:
                    token = self._queue.popleft()
                    if len(self._queue) < 3:
                        threading.Thread(target=self._replenish, daemon=True).start()
                    return token
        return self._minter.mint(amount, legacy=legacy, timeout=timeout, retries=retries)

    @property
    def mint_url(self) -> str:
        return self._minter_url

    def ensure_mint_available(self, timeout: int = 15):
        return self._minter.ensure_mint_available(timeout=timeout)

    def warmup(self, timeout: int = 60):
        return self._minter.warmup(timeout=timeout)

    def is_available(self) -> bool:
        return self._minter.is_available()

    def mint_from_wrong_mint(self, amount: int = 4, timeout: int = 90) -> str:
        return self._minter.mint_from_wrong_mint(amount=amount, timeout=timeout)

    @staticmethod
    def synthetic_wrong_mint_token() -> str:
        return CashuMint.synthetic_wrong_mint_token()
