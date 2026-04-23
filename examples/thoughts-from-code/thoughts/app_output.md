---
id: app-output
title: App output
kind: leaf
links: []
---

# app_output

The `app_output` dataset — produced by the `persist_parquet` call in
`src/app.py`. `croc attack` binds this doc to the source file; `croc
hunt` alerts if the source changes without this doc also being updated.
