#!/bin/bash
echo "Installing requirements..."
pip3 install flask requests pdfminer.six python-docx -q
echo "Starting server..."
python3 app.py
