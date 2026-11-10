"""Validated task-credit ledger primitives (spec Section 11).

Plugins never edit balances.  They return ``SettlementInstruction`` records
composed of ``LedgerOp`` values; the kernel checks them against the
descriptor's settlement rules and applies them atomically here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .records import LedgerOp, Violation


class LedgerError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class Account:
    balance: int
    reserved: int = 0

    @property
    def available(self) -> int:
        return self.balance - self.reserved


@dataclass
class Reservation:
    task_id: str
    principal_id: str
    amount: int
    status: str = "open"  # "open" | "transferred" | "released"
    payee_id: Optional[str] = None


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


class Ledger:
    def __init__(self):
        self.accounts: dict = {}
        self.reservations: dict = {}
        self.log: list = []
        self.initial_total: Optional[int] = None

    # ------------------------------------------------------------ accounts
    def create_account(self, principal_id: str, balance: int) -> None:
        if self.initial_total is not None:
            raise LedgerError("account_after_supply_freeze", f"cannot create {principal_id} after initialization")
        if principal_id in self.accounts:
            raise LedgerError("duplicate_account", f"account {principal_id} already exists")
        if not _is_int(balance) or balance < 0:
            raise LedgerError("invalid_initial_balance", f"invalid initial balance {balance!r}")
        self.accounts[principal_id] = Account(balance=balance)
        self.log.append({"op": "create_account", "principal_id": principal_id, "amount": balance})

    def freeze_supply(self) -> None:
        self.initial_total = self.total()

    def has_account(self, principal_id) -> bool:
        return principal_id in self.accounts

    def total(self) -> int:
        return sum(a.balance for a in self.accounts.values())

    def total_reserved(self) -> int:
        return sum(a.reserved for a in self.accounts.values())

    # ---------------------------------------------------------- primitives
    def reserve(self, task_id: str, principal_id: str, amount: int) -> dict:
        if task_id in self.reservations:
            raise LedgerError("duplicate_reservation", f"task {task_id} already has a reservation")
        account = self.accounts.get(principal_id)
        if account is None:
            raise LedgerError("unknown_account", f"no account {principal_id}")
        if not _is_int(amount) or amount <= 0:
            raise LedgerError("invalid_amount", f"invalid reservation amount {amount!r}")
        if account.available < amount:
            raise LedgerError("insufficient_available_credit", f"{principal_id} cannot reserve {amount}")
        account.reserved += amount
        self.reservations[task_id] = Reservation(task_id, principal_id, amount)
        entry = {"op": "reserve", "task_id": task_id, "principal_id": principal_id, "amount": amount}
        self.log.append(entry)
        return entry

    def _check_op(self, op: LedgerOp) -> Reservation:
        if not isinstance(op, LedgerOp):
            raise LedgerError("malformed_ledger_op", f"not a LedgerOp: {op!r}")
        reservation = self.reservations.get(op.task_id)
        if reservation is None:
            raise LedgerError("no_reservation", f"task {op.task_id} has no reservation")
        if reservation.status != "open":
            raise LedgerError("reservation_not_open", f"reservation for {op.task_id} is {reservation.status}")
        if not _is_int(op.amount) or op.amount != reservation.amount:
            raise LedgerError("amount_mismatch", f"op amount {op.amount!r} != reserved {reservation.amount}")
        if op.kind == "transfer_reserved":
            if op.to_principal_id not in self.accounts:
                raise LedgerError("unknown_payee", f"payee {op.to_principal_id!r} has no account")
            if op.to_principal_id == reservation.principal_id:
                raise LedgerError("self_transfer", "payee equals the reserving principal")
        elif op.kind == "release_reservation":
            if op.to_principal_id is not None:
                raise LedgerError("release_with_payee", "a release must not name a payee")
        else:
            raise LedgerError("unknown_ledger_op", f"unknown op kind {op.kind!r}")
        return reservation

    def apply_ops(self, ops: tuple) -> list:
        """Validate every op first, then apply all of them (atomic)."""
        seen_tasks = set()
        for op in ops:
            self._check_op(op)
            if op.task_id in seen_tasks:
                raise LedgerError("duplicate_task_op", f"two ops settle task {op.task_id}")
            seen_tasks.add(op.task_id)
        entries = []
        for op in ops:
            reservation = self.reservations[op.task_id]
            payer = self.accounts[reservation.principal_id]
            if op.kind == "transfer_reserved":
                payer.balance -= op.amount
                payer.reserved -= op.amount
                self.accounts[op.to_principal_id].balance += op.amount
                reservation.status = "transferred"
                reservation.payee_id = op.to_principal_id
                entry = {
                    "op": "transfer_reserved",
                    "task_id": op.task_id,
                    "from_principal_id": reservation.principal_id,
                    "to_principal_id": op.to_principal_id,
                    "amount": op.amount,
                }
            else:
                payer.reserved -= op.amount
                reservation.status = "released"
                entry = {
                    "op": "release_reservation",
                    "task_id": op.task_id,
                    "principal_id": reservation.principal_id,
                    "amount": op.amount,
                }
            self.log.append(entry)
            entries.append(entry)
        return entries

    # ---------------------------------------------------------- invariants
    def check(self) -> list:
        violations = []
        if self.initial_total is not None and self.total() != self.initial_total:
            violations.append(
                Violation("credit_conservation", f"total credit {self.total()} != initial {self.initial_total}")
            )
        open_by_principal: dict = {}
        for res in self.reservations.values():
            if res.status == "open":
                open_by_principal[res.principal_id] = open_by_principal.get(res.principal_id, 0) + res.amount
        for pid, acct in self.accounts.items():
            if not (_is_int(acct.balance) and _is_int(acct.reserved)):
                violations.append(Violation("non_integer_balance", f"{pid} has a non-integer balance"))
                continue
            if acct.balance < 0 or acct.reserved < 0 or acct.available < 0:
                violations.append(
                    Violation(
                        "negative_balance",
                        f"{pid}: balance={acct.balance} reserved={acct.reserved} available={acct.available}",
                    )
                )
            if acct.reserved > acct.balance:
                violations.append(Violation("reservation_exceeds_balance", f"{pid} reserves more than it holds"))
            if acct.reserved != open_by_principal.get(pid, 0):
                violations.append(
                    Violation("reservation_inconsistent", f"{pid} reserved {acct.reserved} != open reservations")
                )
        return violations

    def snapshot(self) -> dict:
        return {pid: {"balance": a.balance, "reserved": a.reserved} for pid, a in sorted(self.accounts.items())}
