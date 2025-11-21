#!/bin/bash
# Start using gunicorn (for local testing too)
exec gunicorn app:app --bind 0.0.0.0:${PORT:-5000} --workers 2
