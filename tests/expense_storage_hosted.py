"""Both backends behind the actual signed production composition, synthetic only."""
from datetime import date
from decimal import Decimal
import httpx
from pydantic import SecretStr

from line_assistant.history_cloud_run import create_history_cloud_run_app, HistoryConfig
from line_assistant.sheet_dates import serial_to_date
from test_previous_expense_prices import PriceHosted
from test_calendar_factory import values
from test_deployment_configuration import TARGET
from firestore_expense_fake import BusinessFirestore


class StorageHosted(PriceHosted):
    def __init__(self, backend='firestore'):
        self.backend = backend
        self.business = None
        super().__init__()
        self.business = BusinessFirestore(self.google.documents)
        if backend == 'firestore':
            self.seed_business_fixture()

    def configuration(self, enabled=True):
        flags = {'PILOT_PREVIOUS_EXPENSE_PRICES_ENABLED': str(self.prices).lower(),
            'PILOT_HISTORICAL_CATEGORIES_ENABLED': str(self.historical).lower(),
            'PILOT_PROVISIONAL_CATEGORIES_ENABLED': str(self.labels).lower(),
            'PILOT_PROVISIONAL_MAPPING_ENABLED': str(self.mapping).lower(),
            'PILOT_SAVED_EXPENSE_EDITS_ENABLED': str(self.edit_flags).lower(),
            'PILOT_SAVED_CALENDAR_RESCHEDULE_ENABLED': str(self.edit_flags).lower(),
            'PILOT_SAVED_EXPENSE_EDIT_CONCURRENCY': 'firestore_transaction' if self.backend == 'firestore' else 'reviewed_single_writer',
            'PILOT_RESPONSE_REALIZATION_ENABLED': str(self.voice).lower(), 'PILOT_EXPENSE_BACKEND': self.backend,
            'PILOT_FIRESTORE_EXPENSE_SCHEMA_VERIFIED': 'true'}
        return {**values(enabled), **TARGET, **flags,
                'PILOT_LEGACY_SPREADSHEET_ID': TARGET['PILOT_SPREADSHEET_ID'] if self.backend == 'firestore' else ''}

    def seed_business_fixture(self):
        """Test setup only; never an app migration path."""
        target = HistoryConfig.from_mapping(self.configuration()).firestore_expense_target()
        rows = sorted({k[1] for k in self.google.cells if k[0] == 'Database' and k[1] > 1})
        catalog = [{'item': self.google.cells.get(('Database', r, 1), ''),
                    'category': self.google.cells.get(('Database', r, 2), ''),
                    'subcategory': self.google.cells.get(('Database', r, 3), '')} for r in rows]
        catalog = [r for r in catalog if r['item']]
        entries = []
        for r in sorted({k[1] for k in self.google.cells if k[0] == 'All Expenses' and k[1] > 1}):
            item = self.google.cells.get(('All Expenses', r, 2))
            if not item:continue
            entries.append((self.google.cells.get(('All Expenses', r, 5), f'synthetic_legacy_{r}'),
                {'date': serial_to_date(self.google.cells[('All Expenses', r, 1)]).isoformat(), 'item': item,
                 'amount': f"{Decimal(str(self.google.cells[('All Expenses', r, 3)])):.2f}",
                 'note': self.google.cells.get(('All Expenses', r, 4), '')}))
        self.business.seed(target, catalog, entries)
        self.business_target = target

    def app(self, enabled=True):
        return create_history_cloud_run_app(self.configuration(enabled), transport=httpx.MockTransport(self.handle),
            secret_loader=lambda name: SecretStr('synthetic-' + name), clock=lambda: self.now)

    def handle(self, request):
        if self.business is not None and self.business.matches(request):
            return self.business.handle(request)
        return super().handle(request)

    @property
    def expense_writes(self):
        return self.business.commits if self.backend == 'firestore' else self.google.sheet_writes + self.expense_posts + self.mapping_posts
