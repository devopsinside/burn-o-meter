"""``python -m burnometer`` - the fallback ``snapshot.engine_argv`` records when
no ``burnometer`` executable can be found."""

from .cli import main

raise SystemExit(main())
