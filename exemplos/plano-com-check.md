# Plano de exemplo: itens com check

Cinco itens: I1 e I2 não dividem arquivo (mesma onda), I3 depende dos dois, I4 é mecânico e paralelo ao I3, I5 não tem
`check:` e por isso vai para o Sonnet.

- [ ] **I1** Renomear a constante TIMEOUT para TIMEOUT_S em `hub/a.py`
  - check: `python3 -m unittest tests.test_a`
  - files: hub/a.py, tests/test_a.py
  - tipo: codigo
- [ ] **I2** Corrigir o typo no README do módulo b
  - check: `grep -q "corrigido" hub/b/README.md`
  - files: hub/b/README.md
  - tipo: docs
- [ ] **I3** Usar TIMEOUT_S no módulo c
  - check: `python3 -m unittest tests.test_c`
  - files: hub/c.py
  - after: I1, I2
  - tipo: codigo
- [ ] **I4** Ordenar os imports de `hub/d.py`
  - check: `python3 -m py_compile hub/d.py`
  - files: hub/d.py
  - tipo: lint
- [ ] **I5** Decidir o formato do relatório final
  - files: docs/relatorio.md
  - after: I3
  - tipo: docs
