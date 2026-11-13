"""Set by Stop, cleared when a run starts; AI calls check it so a late request cannot outlive Stop."""
import threading

requested = threading.Event()
