from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def test_production_env_generators_trust_only_the_compose_frontend_proxy() -> None:
    install_script = (ROOT / "scripts/install-server.sh").read_text(encoding="utf-8")
    init_script = (ROOT / "scripts/init-prod-env.sh").read_text(encoding="utf-8")
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")

    assert 'put("TRUST_FORWARDED_FOR", "true")' in install_script
    assert '"TRUST_FORWARDED_FOR": "true"' in init_script
    assert "TRUST_FORWARDED_FOR=false" in env_example
