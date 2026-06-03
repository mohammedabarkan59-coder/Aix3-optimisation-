# AIX3 App Balanced Map

Version équilibrée :
- scraping VintedGo par batch ID ;
- optimisation rapide mais équilibrée ;
- carte comme l'ancienne : déplacer points, créer tournée, renommer tournée, exporter CSV ;
- liens Google Maps sans péage.

## Lancement
pip install -r requirements.txt
python -m streamlit run app.py


## Bouton automatique

Le bouton **Actualiser dernière tournée AIX3** utilise :
`https://carrier.vintedgo.com/drivers/point_visits_batches?limit=10&sorting_center_id=288`

Il détecte le batch le plus récent puis scrape directement ses points.
