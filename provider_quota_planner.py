"""Planejador offline de cotas para coleta. NÃO está conectado ao pipeline produtivo.
Uma alocação só é confirmada após registrar o resultado da tentativa.
A cota local é conservadora e não representa saldo oficial do fornecedor.
"""
from dataclasses import dataclass
from typing import Optional, Sequence


@dataclass(frozen=True)
class ProviderAccount:
    provider: str
    credential_id: str
    request_limit: int
    used: int = 0
    available: bool = True
    billing_group: str = ""


class QuotaPlanner:
    def __init__(self, accounts: Sequence[ProviderAccount]):
        self.accounts = list(accounts)
        self.attempted_products = set()
        self._reserved = {}
        for account in self.accounts:
            if account.request_limit < 0 or account.used < 0:
                raise ValueError("Limites/uso não podem ser negativos")
            if account.used > account.request_limit:
                raise ValueError("Uso não pode superar cota")

    def next_account(self, business_key: str) -> Optional[ProviderAccount]:
        if business_key in self.attempted_products or business_key in self._reserved:
            return None
        for a in self.accounts:
            if not a.available or a.used >= a.request_limit:
                continue
            # Mesma billing_group pode compartilhar saldo. Na produção,
            # validar cota do grupo antes de autorizar o request.
            if a.billing_group:
                others = [x for x in self.accounts if x.billing_group == a.billing_group]
                group_limit = min(x.request_limit for x in others)
                group_used = sum(x.used for x in others)
                if group_used >= group_limit:
                    continue
            return a
        return None

    def reserve(self, business_key: str) -> Optional[ProviderAccount]:
        a = self.next_account(business_key)
        if a is None:
            return None
        self._reserved[business_key] = a.credential_id
        return a

    def record_attempt(self, business_key: str) -> None:
        account_id = self._reserved.pop(business_key)
        self.attempted_products.add(business_key)
        self.accounts = [
            ProviderAccount(a.provider, a.credential_id, a.request_limit,
                            a.used + 1 if a.credential_id == account_id else a.used,
                            a.available, a.billing_group)
            for a in self.accounts
        ]

    def cancel_before_request(self, business_key: str) -> None:
        """Só usar quando se tem certeza de que nenhum HTTP request foi enviado."""
        self._reserved.pop(business_key, None)

    def disable(self, credential_id: str) -> None:
        self.accounts = [
            ProviderAccount(a.provider, a.credential_id, a.request_limit,
                            a.used, False if a.credential_id == credential_id else a.available,
                            a.billing_group)
            for a in self.accounts
        ]
