"""Put src/ on the path so tests import the modules the way main.py does."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
