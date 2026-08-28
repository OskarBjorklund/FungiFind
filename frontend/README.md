# FungiFind frontend

Minimal Next.js/TypeScript-klient för en punktbaserad FungiFind-bedömning. Kartan
visar aldrig en heatmap och skannar inte viewporten: varje klick skickar exakt en
WGS84-koordinat, vald art och valt datum till FastAPI.

## Starta lokalt

Starta först backend från repositoryts rot enligt `docs/web_app.md`. Kör sedan:

```powershell
cd frontend
Copy-Item .env.example .env.local
npm ci
npm run dev
```

Öppna `http://localhost:3000`. Ändra värdena i `.env.local` om backend,
baskarta eller startvy ska bytas.

## Kontrollera frontend

```powershell
npm test
npm run lint
npm run build
```

Se `../docs/web_app.md` för API-kontrakt, konfiguration, datakrav och kartkällans
villkor.
