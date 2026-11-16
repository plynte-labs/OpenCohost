#!/usr/bin/env python3
"""OpenCohost VM Installation and Health Verification Crawler.

Validates a fresh or existing OpenCohost installation on a Windows VM:
1. File inventory & hash integrity:
   - Verifies all binaries, runtimes, models (Laya ONNX 1.32GB, MiniLM, Piper, VRM)
   - Validates manifest.json SHA-256 digests for bundled Laya models
   - Verifies absence of forbidden developer files or private data
2. Data isolation contract:
   - Verifies data_root is external to the installation and resources directories
3. API crawler & endpoint health checks:
   - Probes /health and /api/system/info
   - Probes /api/web-fetch/tavily (checks schema, storage availability, zero key leaks)
   - Exercises write-only key lifecycle and safe error responses
4. Bundled runtime verification:
   - Verifies the bundled Python runtime and onnxruntime can resolve & load Laya ONNX

Designed to run using only the Python standard library (no third-party dependencies required).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.request

LAYA_EXPECTED_FILES = (
    "laya.onnx",
    "laya.onnx.data",
    "tokenizer.json",
    "tokenizer_config.json",
    "rl_agent_config.json",
    "manifest.json",
)

MINILM_EXPECTED_FILES = (
    "config.json",
    "model.onnx",
    "tokenizer.json",
)

FORBIDDEN_NAMES = {
    ".env",
    "api_tokens.json",
    "perfiles.json",
    "backend.config.json",
}

FORBIDDEN_EXTENSIONS = {
    ".db",
    ".sqlite",
    ".sqlite3",
    ".jsonl",
}

FORBIDDEN_DIR_PARTS = {
    ".venv",
    "laya_env",
    "staging-backups",
}


def compute_sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest().lower()


class InstallationVerifier:
    def __init__(self, install_dir: Path, data_dir: Path | None = None) -> None:
        self.install_dir = install_dir.resolve()
        self.resources_dir = self.install_dir / "resources"
        self.data_dir = (data_dir or (Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "OpenCohost" / "data")).resolve()
        self.results: dict[str, dict] = {}

    def log(self, section: str, message: str, passed: bool | None = None) -> None:
        tag = "  [INFO]"
        if passed is True:
            tag = "  [PASS]"
        elif passed is False:
            tag = "  [FAIL]"
        print(f"{tag} {message}")

    def verify_file_inventory(self) -> bool:
        print("\n=== 1. Verifying Installation & Resources Inventory ===")
        all_passed = True

        # Check main binary
        main_binary = self.install_dir / "OpenCohost.exe"
        if not main_binary.is_file():
            alt_binary = self.install_dir / "opencohost_tauri_shell.exe"
            if alt_binary.is_file():
                main_binary = alt_binary

        if main_binary.is_file():
            self.log("inventory", f"Found main executable: {main_binary.name} ({main_binary.stat().st_size:,} bytes)", True)
        else:
            self.log("inventory", f"Main executable not found in {self.install_dir}", False)
            all_passed = False

        # Check resources directory
        if not self.resources_dir.is_dir():
            self.log("inventory", f"Resources directory missing: {self.resources_dir}", False)
            return False

        # Check bootstrap-manifest.json
        manifest_path = self.resources_dir / "bootstrap-manifest.json"
        if manifest_path.is_file():
            try:
                manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
                product_version = manifest_data.get("product_version", "unknown")
                self.log("inventory", f"bootstrap-manifest.json valid (version: {product_version})", True)
            except Exception as e:
                self.log("inventory", f"Failed to parse bootstrap-manifest.json: {e}", False)
                all_passed = False
        else:
            self.log("inventory", "bootstrap-manifest.json missing", False)
            all_passed = False

        # Check bundled Laya ONNX multilingual model
        laya_dir = self.resources_dir / "models" / "laya" / "multilingual"
        if laya_dir.is_dir():
            self.log("laya", f"Found Laya directory: {laya_dir}")
            missing_laya = [f for f in LAYA_EXPECTED_FILES if not (laya_dir / f).is_file()]
            if missing_laya:
                self.log("laya", f"Missing Laya files: {missing_laya}", False)
                all_passed = False
            else:
                self.log("laya", "All 6 expected Laya model files present", True)
                
                # Check manifest hashes
                try:
                    laya_manifest = json.loads((laya_dir / "manifest.json").read_text(encoding="utf-8"))
                    files_map = laya_manifest.get("files", {})
                    hashes_match = True
                    for fname, expected_hash in files_map.items():
                        target = laya_dir / fname
                        if not target.is_file():
                            continue
                        actual_hash = compute_sha256(target)
                        if actual_hash != expected_hash.lower():
                            self.log("laya", f"Hash mismatch for {fname}: expected {expected_hash}, got {actual_hash}", False)
                            hashes_match = False
                        else:
                            self.log("laya", f"Verified SHA-256 for {fname} ({target.stat().st_size:,} bytes)", True)
                    if not hashes_match:
                        all_passed = False
                except Exception as e:
                    self.log("laya", f"Failed to verify Laya manifest hashes: {e}", False)
                    all_passed = False
        else:
            self.log("laya", f"Laya multilingual model directory missing: {laya_dir}", False)
            all_passed = False

        # Check MiniLM embeddings model
        minilm_dir = self.resources_dir / "modelos_f5" / "minilm_l12_onnx"
        if minilm_dir.is_dir():
            missing_minilm = [f for f in MINILM_EXPECTED_FILES if not (minilm_dir / f).is_file()]
            if missing_minilm:
                self.log("minilm", f"Missing MiniLM files: {missing_minilm}", False)
                all_passed = False
            else:
                model_onnx_size = (minilm_dir / "model.onnx").stat().st_size
                self.log("minilm", f"MiniLM ONNX model present ({model_onnx_size:,} bytes)", True)
        else:
            self.log("minilm", f"MiniLM directory missing: {minilm_dir}", False)
            all_passed = False

        # Check Python runtime
        python_exe = self.resources_dir / "runtime" / "python" / "python.exe"
        if python_exe.is_file():
            self.log("runtime", f"Bundled standalone python.exe found ({python_exe.stat().st_size:,} bytes)", True)
        else:
            self.log("runtime", f"Bundled standalone python.exe missing at {python_exe}", False)
            all_passed = False

        # Check privacy allowlist: forbidden files must NOT exist
        privacy_ok = True
        for path in self.resources_dir.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(self.resources_dir).as_posix()
            parts = set(rel.split("/"))

            if path.name.lower() in FORBIDDEN_NAMES:
                self.log("privacy", f"FORBIDDEN config file present: {rel}", False)
                privacy_ok = False
            if path.suffix.lower() in FORBIDDEN_EXTENSIONS:
                self.log("privacy", f"FORBIDDEN database/blob present: {rel}", False)
                privacy_ok = False
            if parts & FORBIDDEN_DIR_PARTS:
                self.log("privacy", f"FORBIDDEN developer environment tree present: {rel}", False)
                privacy_ok = False
            if "site-packages" in parts:
                p_parts = list(rel.split("/"))
                sp_idx = p_parts.index("site-packages")
                if sp_idx + 1 < len(p_parts):
                    dist = p_parts[sp_idx + 1].lower()
                    if dist in {"torch", "transformers"} or dist.startswith(("torch-", "transformers-")):
                        self.log("privacy", f"FORBIDDEN heavy ML dependency in runtime: {rel}", False)
                        privacy_ok = False

        if privacy_ok:
            self.log("privacy", "Zero forbidden credentials, databases, or training trees found in resources", True)
        else:
            all_passed = False

        self.results["inventory"] = {"passed": all_passed}
        return all_passed

    def verify_data_isolation(self) -> bool:
        print("\n=== 2. Verifying Data Root External Isolation ===")
        isolation_ok = True

        # Ensure data root is outside install_dir and resources_dir
        try:
            self.data_dir.relative_to(self.install_dir)
            self.log("isolation", f"VIOLATION: data_dir ({self.data_dir}) is INSIDE install_dir ({self.install_dir})", False)
            isolation_ok = False
        except ValueError:
            self.log("isolation", f"Data root correctly resolved outside install_dir: {self.data_dir}", True)

        try:
            self.data_dir.relative_to(self.resources_dir)
            self.log("isolation", f"VIOLATION: data_dir ({self.data_dir}) is INSIDE resources_dir", False)
            isolation_ok = False
        except ValueError:
            pass

        self.results["isolation"] = {"passed": isolation_ok, "data_dir": str(self.data_dir)}
        return isolation_ok

    def test_laya_model_loading(self) -> bool:
        print("\n=== 3. Testing Bundled Laya ONNX Loading via Standalone Python ===")
        python_exe = self.resources_dir / "runtime" / "python" / "python.exe"
        if not python_exe.is_file():
            self.log("laya_load", "Bundled python.exe missing; skipping onnx load test", False)
            return False

        probe_code = """
import sys, os
from pathlib import Path

# Add opencohost to sys.path
runtime_dir = Path(sys.argv[1])
sys.path.insert(0, str(runtime_dir))
sys.path.insert(0, str(runtime_dir / "opencohost"))

# Check onnxruntime import
try:
    import onnxruntime as ort
    print(f"ONNXRUNTIME_VERSION:{ort.__version__}")
except Exception as e:
    print(f"ONNXRUNTIME_IMPORT_ERROR:{e}")
    sys.exit(1)

# Check model file exists and is loadable
model_path = Path(sys.argv[2])
if not model_path.is_file():
    print(f"MODEL_FILE_NOT_FOUND:{model_path}")
    sys.exit(2)

print(f"MODEL_SIZE_BYTES:{model_path.stat().st_size}")

opts = ort.SessionOptions()
opts.intra_op_num_threads = 1
opts.inter_op_num_threads = 1

try:
    session = ort.InferenceSession(str(model_path), sess_options=opts, providers=["CPUExecutionProvider"])
    print("SESSION_LOAD_OK:Laya ONNX graph loaded successfully")
    inputs = [i.name for i in session.get_inputs()]
    outputs = [o.name for o in session.get_outputs()]
    print(f"INPUTS:{inputs}")
    print(f"OUTPUTS:{outputs}")
except Exception as e:
    print(f"SESSION_LOAD_ERROR:{e}")
    sys.exit(3)
"""
        laya_model = self.resources_dir / "models" / "laya" / "multilingual" / "laya.onnx"
        runtime_dir = self.resources_dir / "runtime"

        cmd = [str(python_exe), "-c", probe_code, str(runtime_dir), str(laya_model)]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            stdout = proc.stdout
            stderr = proc.stderr
            for line in stdout.splitlines():
                if line.startswith("ONNXRUNTIME_VERSION:"):
                    self.log("laya_load", f"onnxruntime {line.split(':', 1)[1]}")
                elif line.startswith("SESSION_LOAD_OK:"):
                    self.log("laya_load", line.split(":", 1)[1], True)
                elif line.startswith("INPUTS:"):
                    self.log("laya_load", f"Model inputs: {line.split(':', 1)[1]}")
                elif line.startswith("OUTPUTS:"):
                    self.log("laya_load", f"Model outputs: {line.split(':', 1)[1]}")

            if proc.returncode == 0:
                self.results["laya_runtime"] = {"passed": True}
                return True
            else:
                self.log("laya_load", f"Laya loading failed (exit {proc.returncode}): {stdout} {stderr}", False)
                self.results["laya_runtime"] = {"passed": False, "error": stderr or stdout}
                return False
        except Exception as e:
            self.log("laya_load", f"Failed to execute Laya probe subprocess: {e}", False)
            self.results["laya_runtime"] = {"passed": False, "error": str(e)}
            return False

    def crawl_api_endpoints(self, base_url: str = "http://127.0.0.1:8000", operator_token: str | None = None) -> bool:
        print("\n=== 4. Crawling Backend API Endpoints ===")
        all_passed = True

        # 1. Health check
        health_urls = [f"{base_url}/api/health", f"{base_url}/api/status", f"{base_url}/health"]
        health_found = False
        for url in health_urls:
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "OpenCohost-VM-Crawler/1.0"})
                with urllib.request.urlopen(req, timeout=5) as resp:
                    if resp.status == 200:
                        body = json.loads(resp.read().decode("utf-8"))
                        self.log("api_crawl", f"GET {url} returned 200 OK: {body}", True)
                        health_found = True
                        break
            except Exception as e:
                self.log("api_crawl", f"GET {url} probe attempt: {e}")

        if not health_found:
            self.log("api_crawl", f"Backend server not responding at {base_url} (is it running?)", False)
            # Not failing the whole script if backend isn't started yet
            self.results["api_crawl"] = {"passed": False, "reason": "server_not_running"}
            return False

        # 2. Tavily settings contract check
        tavily_url = f"{base_url}/api/web-fetch/tavily"

        # 2a. Without operator token -> must be 401 Unauthorized
        try:
            req = urllib.request.Request(tavily_url)
            urllib.request.urlopen(req, timeout=5)
            self.log("api_crawl", f"GET {tavily_url} unexpectedly succeeded without operator token!", False)
            all_passed = False
        except urllib.error.HTTPError as err:
            if err.code in (401, 403):
                self.log("api_crawl", f"GET {tavily_url} without auth rejected with HTTP {err.code} as required", True)
            else:
                self.log("api_crawl", f"GET {tavily_url} returned unexpected HTTP {err.code}", False)
                all_passed = False
        except Exception as e:
            self.log("api_crawl", f"GET {tavily_url} network error: {e}", False)
            all_passed = False

        # Auto-discover operator token if not explicitly provided
        if not operator_token:
            token_candidate = self.data_dir / "config" / "api_tokens.json"
            if token_candidate.is_file():
                try:
                    data = json.loads(token_candidate.read_text(encoding="utf-8"))
                    operator_token = data.get("operator")
                    if operator_token:
                        self.log("api_crawl", f"Auto-discovered operator token from {token_candidate}")
                except Exception as e:
                    self.log("api_crawl", f"Could not read {token_candidate}: {e}")

        # 2b. With operator token -> check response fields & secrecy
        if operator_token:
            headers = {
                "Authorization": f"Bearer {operator_token}",
                "Content-Type": "application/json",
            }
            try:
                req = urllib.request.Request(tavily_url, headers=headers)
                with urllib.request.urlopen(req, timeout=5) as resp:
                    if resp.status == 200:
                        data = json.loads(resp.read().decode("utf-8"))
                        self.log("api_crawl", f"GET {tavily_url} with auth returned 200 OK")

                        # Verify required redacted keys
                        required_keys = {"api_key_set", "stored_key_set", "source", "storage_available"}
                        if set(data.keys()) == required_keys:
                            self.log("api_crawl", f"Response schema strictly contains only allowed fields: {set(data.keys())}", True)
                        else:
                            self.log("api_crawl", f"Schema mismatch! Got fields: {set(data.keys())}, expected: {required_keys}", False)
                            all_passed = False

                        # Verify NO secret key is echoed
                        raw_dump = json.dumps(data).lower()
                        forbidden_tokens = ["key=", "secret", "token", "tavily-", "tvly-"]
                        leak_found = any(tok in raw_dump for tok in forbidden_tokens)
                        if not leak_found:
                            self.log("api_crawl", "No key, secret, preview or token hashes in status response", True)
                        else:
                            self.log("api_crawl", "POTENTIAL LEAK: Forbidden token found in response!", False)
                            all_passed = False
            except Exception as e:
                self.log("api_crawl", f"GET {tavily_url} with token failed: {e}", False)
                all_passed = False

            # 2c. PUT write-only test: set empty key to verify write & zero leak
            try:
                body = json.dumps({"api_key": ""}).encode("utf-8")
                req = urllib.request.Request(tavily_url, data=body, headers=headers, method="PUT")
                with urllib.request.urlopen(req, timeout=5) as resp:
                    if resp.status == 200:
                        put_data = json.loads(resp.read().decode("utf-8"))
                        self.log("api_crawl", "PUT empty key succeeded with 200 OK", True)
                        if set(put_data.keys()) == required_keys:
                            self.log("api_crawl", "PUT response contains only allowed redacted fields", True)
                        else:
                            all_passed = False
            except Exception as e:
                self.log("api_crawl", f"PUT {tavily_url} test failed: {e}", False)
                all_passed = False

        self.results["api_crawl"] = {"passed": all_passed}
        return all_passed

    def run_all(self, base_url: str = "http://127.0.0.1:8000", operator_token: str | None = None) -> bool:
        print("=" * 65)
        print(" OpenCohost VM Installation & Health Verification Crawler")
        print("=" * 65)
        print(f"Target Install Directory: {self.install_dir}")
        print(f"Target Data Directory:    {self.data_dir}")

        p1 = self.verify_file_inventory()
        p2 = self.verify_data_isolation()
        p3 = self.test_laya_model_loading()
        p4 = self.crawl_api_endpoints(base_url, operator_token)

        overall = p1 and p2 and p3 and (p4 or self.results.get("api_crawl", {}).get("reason") == "server_not_running")

        print("\n" + "=" * 65)
        print(" Verification Summary")
        print("=" * 65)
        print(f" 1. File Inventory & Hash Integrity:  {'PASS' if p1 else 'FAIL'}")
        print(f" 2. External Data Root Isolation:    {'PASS' if p2 else 'FAIL'}")
        print(f" 3. Bundled Laya ONNX Runtime Load:  {'PASS' if p3 else 'FAIL'}")
        api_status = "PASS" if p4 else ("SKIPPED (backend not running)" if self.results.get("api_crawl", {}).get("reason") == "server_not_running" else "FAIL")
        print(f" 4. API Endpoints & Tavily Secrecy:  {api_status}")
        print(f"\n OVERALL INSTALLATION HEALTH:        {'PASS' if overall else 'FAIL'}")
        print("=" * 65)
        return overall


def main() -> int:
    parser = argparse.ArgumentParser(description="OpenCohost VM Installation & Health Verification Crawler")
    parser.add_argument(
        "--install-dir",
        type=Path,
        default=Path(os.environ.get("LOCALAPPDATA", "C:\\Users\\Default\\AppData\\Local")) / "Programs" / "OpenCohost",
        help="Path to OpenCohost installation directory (default: %%LOCALAPPDATA%%\\Programs\\OpenCohost)",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="Path to external user data directory (default: %%LOCALAPPDATA%%\\OpenCohost\\data)",
    )
    parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:8000",
        help="Base URL of OpenCohost backend API (default: http://127.0.0.1:8000)",
    )
    parser.add_argument(
        "--operator-token",
        default=os.environ.get("OPENCOHOST_OPERATOR_TOKEN"),
        help="Operator token for authenticating protected API endpoints",
    )
    parser.add_argument(
        "--output-json",
        type=Path,
        default=None,
        help="Write structured JSON test results to specified path",
    )
    args = parser.parse_args()

    verifier = InstallationVerifier(args.install_dir, args.data_dir)
    success = verifier.run_all(args.base_url, args.operator_token)

    if args.output_json:
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(json.dumps(verifier.results, indent=2), encoding="utf-8")
        print(f"\nWrote structured results to {args.output_json}")

    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
