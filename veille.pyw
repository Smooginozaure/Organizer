import os
import sys

# Lancé sans fenêtre par le Planificateur de tâches (veille automatique).
ROOT = os.path.dirname(os.path.abspath(__file__))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

from mailarchive.veille import executer  # noqa: E402

sys.exit(executer())
