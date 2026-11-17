"""Search-provider capability registry; informational only, no API calls or credential disclosure.

Statuses describe actual integration in pipeline_produtos.py, not lab experiments.
"""
from __future__ import annotations
from dataclasses import dataclass

@dataclass(frozen=True)
class ProviderCapability:
    key: str
    label: str
    status: str
    execution: str
    manual: bool
    automatic: bool
    notes: str = ""

PROVIDERS: tuple[ProviderCapability, ...] = (
    ProviderCapability("tavily", "TAVILY", "INTEGRADO", "LOCAL_API", True, True),
    ProviderCapability("serper", "SERPER", "INTEGRADO", "LOCAL_API", True, True),
    ProviderCapability("brightdata", "BRIGHT DATA", "INTEGRADO", "LOCAL_API", True, True, "Slots e limites"),
    ProviderCapability("scraperapi", "SCRAPERAPI", "LABORATORIO", "LOCAL_API", False, False, "Adaptador experimental; sem roteamento de producao"),
    ProviderCapability("octoparse", "OCTOPARSE", "LABORATORIO", "REMOTE_TASK", False, False, "MCP e mapeamento de tarefas; sem coleta de producao"),
    ProviderCapability("openserp", "OPENSERP", "LABORATORIO", "GITHUB_ACTIONS", False, False, "Lotes remotos; sem importacao e roteamento de producao"),
)

def manual_choices() -> tuple[ProviderCapability, ...]:
    return tuple(p for p in PROVIDERS if p.manual)

def automatic_choices() -> tuple[ProviderCapability, ...]:
    return tuple(p for p in PROVIDERS if p.automatic)

def display_menu(allow_auto: bool = False) -> None:
    print("\nAPI DE COLETA / SEARCH")
    for i, provider in enumerate(manual_choices(), 1):
        print(f"{i} - {provider.label}")
    if allow_auto:
        print(f"{len(manual_choices()) + 1} - AUTOMATICO (provedores integrados, conforme slots/limites)")
    experimental = [p for p in PROVIDERS if not p.manual]
    if experimental:
        print("\nINTEGRACOES DISPONIVEIS NO REPOSITORIO (AINDA NAO HABILITADAS NO PIPELINE)")
        for p in experimental:
            print(f"- {p.label}: {p.status} / {p.execution} — {p.notes}")

def validate_registry(routable: set[str]) -> None:
    keys = [p.key for p in PROVIDERS]
    if len(keys) != len(set(keys)):
        raise ValueError("DUPLICATE_PROVIDER_KEYS")
    configured = {p.key for p in manual_choices()}
    if configured != routable:
        raise ValueError(f"MANUAL_ROUTING_MISMATCH: {configured ^ routable}")
    for p in PROVIDERS:
        if p.status != "INTEGRADO" and (p.manual or p.automatic):
            raise ValueError(f"EXPERIMENTAL_PROVIDER_ENABLED: {p.key}")
