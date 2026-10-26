"""Regenerate the Word templates in `apps/kits/templates_docs/` from `docx_templates.py`."""

from typing import Any

from django.core.management.base import BaseCommand

from apps.kits import docx_templates
from apps.kits.documents import DOC_COVER, DOC_CV, DOCX_TEMPLATES, TEMPLATE_DIR


class Command(BaseCommand):
    help = "Rebuild the .docx templates used for CV and cover letter downloads."

    def handle(self, *args: Any, **options: Any) -> None:
        docx_templates.build_cv_template(TEMPLATE_DIR / DOCX_TEMPLATES[DOC_CV])
        docx_templates.build_cover_letter_template(TEMPLATE_DIR / DOCX_TEMPLATES[DOC_COVER])
        self.stdout.write(self.style.SUCCESS("Document templates rebuilt."))
