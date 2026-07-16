Skrypt prezentacji — RavenDB jako pamięć robocza agenta AI
OTWARCIE (hook, ~1 min)
Zanim zaczniemy — kto z was uruchomił już agenta AI i wpuścił go do produkcji? A kto potem dostał rachunek za OpenAI API i trochę się zdziwił?

To, co wam pokażę, to nie jest demo "AI robi fajne rzeczy". To demo o pieniądzach. O dwóch pozycjach budżetowych, których nikt nie kalkuluje na początku: koszcie tokenów i koszcie egresu. Obie rosną liniowo z ruchem. I obie można prawie wyeliminować — bez zmiany modelu, bez zmiany inference providera.

PROBLEM (use case, ~2 min)
Use case: asystent do wyszukiwania lotów. Użytkownik pyta: "znajdź mi tanie loty z Warszawy do Londynu, interesuje mnie hidden city".

Hidden city to trик cenowy — lot WAW→JFK przez LHR kosztuje 1650 PLN, a bezpośredni WAW→LHR kosztuje 2800 PLN. Kupujesz bilet do Nowego Jorku i wysiadasz w Londynie. Linie lotnicze tego nie lubią, ale jest legalne. Agent musi to wykryć, pokazać użytkownikowi, i nigdy nie zaautomatyzować bookingu — to jest ryzyko prawne.

Ale wróćmy do infrastruktury. Żeby agent mógł odpowiedzieć na takie pytanie, musi mieć kontekst: ceny tras, historię rozmowy, preferencje użytkownika. Skąd to dostaje?

ETAP 1 — Naiwne podejście (~3 min)
Najprościej: wywołujesz Travelpayouts API, dostajesz odpowiedź — setki rekordów cen, tras, przewoźników w surowym JSONie. Wrzucasz to wszystko do prompta LLM i pytasz "co o tym myślisz?"

Wynik: 40 000–100 000 tokenów na jedno zapytanie. Przy cenie $0.15 za milion tokenów input (gpt-4o-mini), to $0.006 za request. Przy 1000 zapytań dziennie — $6/dzień. Przy 10 000 — $60/dzień.

I jeszcze jeden problem: agent jest bezstanowy. Następne pytanie użytkownika — "a co ze mną carry-on only?" — i agent nie pamięta poprzedniej rozmowy. Musisz ponownie wysłać wszystko od nowa.

ETAP 2 i 3 — "Dojrzałe" stosy (~3 min)
Okej, konteneryzujemy. Dodajemy Redis jako cache, Postgres do historii rozmów, Celery do pollowania cen co 5 minut. Koszt tokenów spada trochę — do $105/dzień — ale nie dlatego, że wysyłamy mniej. Redis zwraca ten sam 250 KB blob do promptu. Token cost prawie bez zmian.

Idziemy dalej. Kubernetes, Elasticsearch do wyszukiwania, pgvector do semantic search, Kafka do event-driven price watch. Teraz mamy pięć osobnych systemów do utrzymania: pięć dashboardów, pięć strategii backup, trzy języki zapytań. Infra miesięcznie: $1190. LLM koszt: $60/dzień. Lepiej, ale stack jest ogromny.

I kluczowa obserwacja: egres wciąż opuszcza klaster na każdym retrievalu. Dane wychodzą z klastra, wchodzą do agenta, wychodzą do OpenAI. Płacisz za każdy byte.

ETAP 4 — RavenDB in-cluster (~5 min)
A teraz to samo, ale inaczej.

(pokaż diagram architektury)

Agent działa w Kubernetes. RavenDB działa w tym samym klastrze — jako operator, trzy nody, HA out of the box. Dane przylatują z Travelpayouts co 6 godzin przez CronJob. Na cache miss agent odpytuje Travelpayouts live API — zdarza się to w ~5% przypadków.

Użytkownik pyta o loty. Agent nie wkłada danych do prompta. Zamiast tego model wywołuje narzędzie — search_routes(). RavenDB odpowiada lokalnie: pre-strukturyzowany dokument z origin, destination, ceną, hidden city score. Około 400 tokenów. Tool result wraca do tego samego API call.

Na zewnątrz klastra wychodzi tylko prompt użytkownika. 100 tokenów. Zero retrieval egresu.

(pokaż measure_tokens.py output)

1500 tokenów na request zamiast 40 000. Koszt LLM: $0.47/dzień zamiast $6. To jest 92% redukcja — bez zmiany modelu.

LIVE DEMO — trzy momenty (~5 min)
1. Zero egress

(uruchom measure_egress.py) — tu widzicie: retrieval egress: 0 KB. Tylko prompt wychodzi z klastra.

2. Price-drop push bez pollowania

(zmień dokument trasy w RavenDB Studio) — RavenDB Subscription wykrywa zmianę dokumentu i natychmiast pushuje alert do workera. Żadnego schedulera, żadnego Kafki, żadnego Celery. Jeden produkt.

3. Pod restart resilience

(zabij pod agenta mid-conversation) — restart. Kontynuujemy rozmowę dokładnie tam, gdzie skończyliśmy. Historia konwersacji żyje w RavenDB jako dokument, nie w pamięci poda.

CO RAVENDB ZASTĘPUJE (~2 min)
Jeden klaster zastępuje:

Redis — sesje jako dokumenty z TTL
Postgres — historia konwersacji, indeksowalna, queryowalna
Elasticsearch — full-text search na trasach i lotniskach
pgvector — vector fields na dokumentach tras, semantic similarity wbudowane
Kafka/Celery — RavenDB Subscriptions pushują na zmianę dokumentu
Infra miesięcznie: $90 zamiast $1190.

OPERATOR (~1 min)
Jak to działa na k8s? Jeden YAML:


apiVersion: ravendb.com/v1alpha1
kind: RavenDBCluster
spec:
  nodes: 3
  storage: 50Gi
  tlsMode: ClusterExternalAccess
kubectl apply i gotowe. Operator bootstrapuje klaster, generuje certyfikaty TLS, zarządza Raft quorum podczas rolling upgrades — nigdy nie schodzi poniżej (n/2)+1 żywych nodów. Admission webhooks blokują nieprawidłowe konfiguracje zanim cokolwiek się stanie.

ZAMKNIĘCIE (~1 min)
Inference zostawiliśmy na zewnątrz — OpenAI API — żeby pokazać, że oszczędności są wyłącznie z kontekstu. Nie z tego, gdzie liczy model. Z tego, co przestajemy wysyłać.

Jeśli macie GPU w klastrze, llm-d albo vLLM jako drop-in replacement eliminuje ostatni egres. Interface narzędzi agenta się nie zmienia.

Summary: 40 000 tokenów → 1 500. $6/dzień → $0.47. Pięć systemów → jeden. Jeden kubectl apply.

PYTANIA — przydatne odpowiedzi z góry
"Dlaczego nie po prostu Redis?"

Redis daje TTL cache, ale nie daje vector search, full-text, subscriptions ani trwałości konwersacji. I osobno musisz Postgres i Kafka. To są trzy produkty, trzy koszty operacyjne.

"Co z vendor lockiem?"

RavenDB ma otwarte API klienckie. Narzędzia agenta to thin wrapper — search_routes, get_live_price. Swap za inny store to podmiana implementacji narzędzia, nie przepisanie agenta.

"Co z większym ruchem?"

Tabela w prezentacji: przy 100 000 req/dzień saving wynosi $11 550/dzień. Trzy nody RavenDB skalują poziomo — dokładasz nody przez operator.
