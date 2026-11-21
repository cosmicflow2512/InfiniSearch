# InfiniSearch

Freie Indexer-Suche für [InfiniDysk](https://github.com/infinidysk/infinidysk) 1.x – ohne IMDb, ohne manuelles Mount.

- Suchbegriff eingeben → alle Treffer deiner in InfiniDysk eingetragenen Indexer
- **▶ Play**: NZB wird automatisch eingereiht und im Browser abgespielt
- **Weiter / Zurück**, Spulen, Autoplay, Tastatur (`N` `P` `F` `←` `→` `Leertaste`)
- **★ Favoriten**: NZB-Datei wird lokal gespeichert und kann jederzeit neu abgespielt werden
- **Privatsphäre**: Suchverlauf und alle von InfiniSearch geladenen NZBs (inkl. Mount) löschen – Favoriten bleiben

## Voraussetzungen
- InfiniDysk **1.x** (`ghcr.io/infinidysk/infinidysk`) – 2.x hat keine Indexer-Suche
- Indexer in InfiniDysk unter *Settings → Indexers* eingetragen
- `API_KEY` = `FRONTEND_BACKEND_API_KEY` der InfiniDysk-Instanz

## Unraid
Template: `unraid/infinisearch.xml` nach `/boot/config/plugins/dockerMan/templates-user/my-InfiniSearch.xml` kopieren, dann *Docker → Add Container → InfiniSearch*.

## Umgebungsvariablen
| Variable | Standard | Beschreibung |
|---|---|---|
| `INFINIDYSK_URL` | `http://192.168.0.20:3008` | InfiniDysk-Adresse |
| `API_KEY` | – | `FRONTEND_BACKEND_API_KEY` von InfiniDysk |
| `CATEGORY` | `movies` | SAB-Kategorie für eingereihte NZBs |
| `INFINIDYSK_PUBLIC_URL` | = `INFINIDYSK_URL` | Adresse, unter der der Browser InfiniDysk erreicht |
| `SEARCH_LIMIT` | `100` | Treffer pro Indexer |
| `APP_USER` / `APP_PASSWORD` | leer | Optionaler Basic-Auth-Login |
| `DATA_DIR` | `/data` | Favoriten, Verlauf |
| `PORT` | `8090` | Web-Port |

## Verwendete InfiniDysk-Endpunkte (v1.7.1)
`/api/search-indexers`, `/api/download-nzb`, `/api/list-webdav-directory`, SABnzbd-API (`addurl`, `addfile`, `queue`, `history`, `delete`), signierte `/view`-Links (HMAC-SHA256 mit `FRONTEND_BACKEND_API_KEY`).
