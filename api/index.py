"""
Vercel WSGI entry point
"""
import sys, os

# Add parent directory to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# Import the Flask app
from app import app as application

# For Vercel
app = application
