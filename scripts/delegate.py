#!/usr/bin/env python3
"""
VoiceAI — OpenCode Agent Delegation Script (Python Version)
Usage:
  python scripts/delegate.py run "Revisá core/llm_engine.py"
  python scripts/delegate.py review "src/core/"
  python scripts/delegate.py feature "Agregá cancelación de TTS"
  python scripts/delegate.py session-start
  python scripts/delegate.py safe-stage
"""

import sys
import os
import shutil
import subprocess

def print_step(text: str):
    # Cyan-colored header
    print(f"\n>> \033[36m{text}\033[0m")

def print_ok(text: str):
    # Green success message
    print(f"   \033[32m{text}\033[0m")

def print_warn(text: str):
    # Yellow warning message
    print(f"   \033[33m{text}\033[0m")

def print_error(text: str):
    # Red error message
    print(f"\033[31mERROR: {text}\033[0m")

def check_opencode_installed() -> bool:
    return shutil.which("opencode") is not None

def get_opencode_version() -> str:
    try:
        res = subprocess.run(["opencode", "--version"], capture_output=True, text=True, check=True)
        return res.stdout.strip()
    except Exception:
        return "Desconocida"

def main():
    if len(sys.argv) < 2:
        print_warn("Uso: python scripts/delegate.py [run|review|feature|session-start|safe-stage] [args...]")
        sys.exit(1)

    action = sys.argv[1]
    args = sys.argv[2:]

    if not check_opencode_installed():
        print_error("opencode no está instalado o no está en el PATH.")
        print_warn("Instalalo desde: https://opencode.ai/docs/")
        sys.exit(1)

    project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    agent_name = "voiceai"

    print_step(f"OpenCode detectado: {get_opencode_version()}")

    cmd = ["opencode", "run", "--dir", project_dir]

    # Append dangerously-skip-permissions for autonomous background operations
    # If the user runs from their own interactive terminal, they can choose to skip or confirm.
    skip_permissions_flag = "--dangerously-skip-permissions"

    if action == "run":
        prompt = " ".join(args)
        if not prompt:
            print_warn("Sin prompt. Uso: python scripts/delegate.py run 'tu prompt'")
            sys.exit(1)
        print_step(f"Delegando a {agent_name}...")
        cmd.extend(["--agent", agent_name, prompt])
        
    elif action == "review":
        target = " ".join(args) if args else "src/"
        print_step(f"Delegando revisión de '{target}'...")
        prompt = f"Revisá el código en {target}. Reportá hallazgos en JSON con file, line, severity (CRÍTICO/WARNING/SUGERENCIA), skill, finding, fix_suggestion. NO modifiques archivos."
        cmd.extend(["--agent", agent_name, prompt])

    elif action == "feature":
        desc = " ".join(args)
        if not desc:
            print_warn("Describí la feature. Uso: python scripts/delegate.py feature 'descripción'")
            sys.exit(1)
        print_step("Delegando scaffolding de feature...")
        prompt = f"Creá una feature nueva: {desc}. Seguí stress-first TDD: tests rojos primero, implementación mínima, intentá romper tus tests. No commitear."
        cmd.extend(["--agent", agent_name, prompt])

    elif action == "session-start":
        print_step("Iniciando sesión VoiceAI...")
        cmd.extend(["--command", "session-start"])

    elif action == "safe-stage":
        print_step("Safe stage check...")
        cmd.extend(["--command", "safe-stage-check"])

    else:
        print_error(f"Acción no reconocida: {action}")
        print_warn("Uso: python scripts/delegate.py [run|review|feature|session-start|safe-stage] [args...]")
        sys.exit(1)

    # Execute
    try:
        # Run process interactively so the user can see everything in real time
        subprocess.run(cmd, check=True)
        print_ok("Listo.")
    except subprocess.CalledProcessError as exc:
        print_error(f"Error al ejecutar opencode: {exc}")
        sys.exit(exc.returncode)

if __name__ == "__main__":
    main()
