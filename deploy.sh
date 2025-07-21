#!/usr/bin/env bash
set -e
python script.py "$@"        # arguments pass-through
rm -rf docs
cp -R demo_site docs
git add docs preview.json
git commit -m "deploy $(date +%F_%T)" || echo "Nothing to commit"
git push