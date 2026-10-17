"""Synthetic taxonomy and atomic enrollment ledger; no credentials/network."""
from types import SimpleNamespace
from .adapters import MockSheets, Effects, Conflict
from .enrollment import Enrollment
from .taxonomy import Taxonomy
from .catalog import normalized

class ConversationSheets(MockSheets):
    def __init__(self, layout, rows):
        super().__init__(layout)
        self.rows = list(rows)
        self.enrollments = Effects()
        self.atomic_batches = 0
        self.target = SimpleNamespace(spreadsheet_id='synthetic-conversation')

    def taxonomy(self): return Taxonomy(self.rows)
    def catalog(self): return self.taxonomy().catalog

    def enroll_and_add(self, payload, key):
        plan = Enrollment.model_validate(payload).checked()
        def apply():
            taxonomy = self.taxonomy(); additions = []
            for row in plan.catalog:
                if (row.category, row.subcategory) not in {(r.category, r.subcategory) for r in taxonomy.rows}:
                    raise Conflict('Pair removed')
                found = [r for r in taxonomy.rows if normalized(r.item) == normalized(row.item)]
                wanted = (row.item, row.category, row.subcategory)
                if found and [(r.item,r.category,r.subcategory) for r in found] != [wanted]:
                    raise Conflict('Mapping changed')
                if not found: additions.append(wanted)
            if any(e.item not in {r.item for r in taxonomy.rows} | {r[0] for r in additions} for e in plan.entries):
                raise Conflict('Other item unresolved')
            # Both writes occur under the owner state lock. No injected boundary inside this mock batch.
            receipt = super(ConversationSheets, self).add(plan.entries, key)
            self.rows.extend(additions); self.atomic_batches += 1
            return receipt
        return self.enrollments.once(key, payload, apply)

    def reconcile_enrollment(self, key, payload): return self.enrollments.reconcile(key, payload)
