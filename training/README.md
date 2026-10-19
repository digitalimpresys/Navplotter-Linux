# Un modello per il mare

Il modello che il Pi usa all'inizio (`yolov8s_h8.hef` del pacchetto hailo-models) è addestrato su COCO: conosce
barche e persone, ma non boe, navi, ostacoli galleggianti, e non ha mai visto il mare di notte. Qui c'è quello che
serve per addestrarne uno nostro, con queste classi (`classes.json`):

| classe     | sulla pagina   | cosa comprende                                            |
|------------|----------------|-----------------------------------------------------------|
| `boat`     | barca          | barche a motore, gommoni, pescherecci, kayak, moto d'acqua |
| `ship`     | nave           | navi, traghetti, petroliere, rimorchiatori                |
| `sailboat` | barca a vela   | barche a vela, yacht, catamarani                           |
| `person`   | persona        | persone in acqua, nuotatori, uomo a mare                   |
| `buoy`     | boa            | boe di segnalazione e di ormeggio, salvagenti              |
| `obstacle` | ostacolo       | tronchi, container, reti, oggetti galleggianti             |

Le ultime tre sono disegnate in rosso. Le classi si possono cambiare in `classes.json`: i sinonimi servono a
tradurre i nomi dei dataset nei nostri.

## Il percorso

1. **Registrare** con la telecamera del Pi.
2. **Etichettare** le registrazioni.
3. **Addestrare** su Colab, insieme ai dataset pubblici.
4. **Compilare** per l'Hailo-8 su un PC Linux.
5. **Copiare** il modello sul Pi.

### 1. Registrare

Nella pagina Telecamera, **Registra** salva un fotogramma al secondo (al massimo un'ora per registrazione, e si
ferma da sola se il disco si riempie). Sotto compare l'elenco delle registrazioni: **Scarica** dà uno zip dei
fotogrammi, **Elimina** la cancella. Conviene registrare in condizioni diverse: giorno, tramonto, notte, mare calmo
e mosso, con la vista notturna accesa e spenta, perché il modello impara solo quello che vede. Anche i tratti di
mare senza niente servono: insegnano al modello a non vedere barche nelle onde.

### 2. Etichettare

Si disegnano i rettangoli attorno agli oggetti con uno di questi programmi, tutti gratuiti:

- **Roboflow** (roboflow.com), nel browser: il più comodo, ed esporta direttamente nel formato giusto.
- **CVAT** (app.cvat.ai), nel browser.
- **Label Studio**, sul tuo computer (`pip install label-studio`).

Per non partire da zero, `prelabel.py` fa una prima passata col modello COCO (trova barche e persone) e dopo il
primo addestramento col tuo modello (trova tutto): si correggono i rettangoli invece di disegnarli.

```bash
pip install ultralytics
python3 prelabel.py 20261019-201500/
python3 prelabel.py 20261019-201500/ -m best.pt     # col modello già addestrato
```

Accanto a ogni fotogramma scrive un `.txt` in formato YOLO, più un `classes.txt`: la cartella si importa così com'è
in Roboflow, CVAT ("YOLO 1.1") o Label Studio. Dopo le correzioni si esporta in formato **YOLO**.

### 3. Addestrare

Apri `train_colab.ipynb` in Google Colab (dal sito di Colab: *File → Apri notebook → GitHub*, e incolla l'indirizzo
del file su GitHub), scegli il runtime con GPU ed esegui le celle in ordine. Il notebook:

- prende le tue registrazioni etichettate;
- prende i dataset pubblici che gli indichi:
  - [Roboflow Universe](https://universe.roboflow.com): cerca *buoy*, *boat*, *marine obstacle*, *man overboard*;
  - [SeaDronesSee](https://seadronessee.cs.uni-tuebingen.de): persone in acqua e barche, riprese dall'alto;
  - [LaRS](https://lojzezust.github.io/lars-dataset/) e MODS: ostacoli ripresi da bordo, i più simili alla nostra telecamera;
- li unisce con `merge_datasets.py`;
- addestra un YOLOv8s partendo da quello COCO;
- ti fa scaricare `navplotter-marine.zip`, con `best.onnx`, `best.pt`, `classes.json` e le immagini di
  calibrazione.

`merge_datasets.py` funziona anche da solo, per esempio sul tuo computer:

```bash
python3 merge_datasets.py -o dataset  boe-roboflow/  seadronessee/annotations/instances_train.json  mie-registrazioni/
```

Alla fine stampa quante immagini e quanti oggetti per classe ha preso, e quali classi dei dataset ha scartato:
se tra quelle ce n'è una che ci serve, si aggiunge ai sinonimi in `classes.json`.

Quanti dati servono: qualche centinaio di esempi per classe dà già un risultato utilizzabile, qualche migliaio uno buono.
Le tue registrazioni contano più di tutto il resto, perché sono la tua telecamera e il tuo mare.

### 4. Compilare per l'Hailo

L'Hailo-8 non esegue l'ONNX direttamente: va compilato in un `.hef`. Il compilatore di Hailo gira solo su un PC
**Linux x86-64**, oppure su Windows con WSL2. Non gira sul Pi né su un Mac con chip Apple.

1. Registrati sul [Hailo Developer Zone](https://hailo.ai/developer-zone/), gratuito.
2. Scarica la **Hailo AI Software Suite** (immagine Docker). Prendi la versione che corrisponde alla HailoRT del
   Pi (`hailortcli --version` sul Pi): con versioni diverse il Pi non carica il modello.
3. Dentro la Suite, scompatta lo zip del notebook ed esegui:

```bash
unzip navplotter-marine.zip -d navplotter-marine && cd navplotter-marine
/percorso/di/Navplotter-Linux/training/compile.sh best.onnx calib/ classes.json
```

Ci vuole da mezz'ora a qualche ora. Il risultato sono due file, `navplotter-marine-<data>.hef` e `.json`.
Lo script compila per l'Hailo-8: per l'AI Kit da 13 TOPS usa `HAILO_ARCH=hailo8l`.

### 5. Copiare sul Pi

```bash
scp navplotter-marine-*.hef navplotter-marine-*.json pi@<indirizzo-del-pi>:/tmp/
# sul Pi:
sudo install -o navplotter -g navplotter -m 644 -D -t /var/lib/navplotter/models /tmp/navplotter-marine-*
sudo systemctl restart navplotter-camera
```

La telecamera usa il `.hef` più recente in `/var/lib/navplotter/models`, con i nomi delle classi del `.json` che
ha lo stesso nome. Sotto il video la riga d'informazioni mostra il modello in uso. Per tornare al modello COCO
basta togliere il `.hef` da quella cartella e riavviare il servizio.

## I file

| file                 | cosa fa                                                              |
|----------------------|----------------------------------------------------------------------|
| `classes.json`       | le classi, i nomi in italiano, quelle in rosso, i sinonimi            |
| `merge_datasets.py`  | unisce dataset YOLO e COCO in uno solo con le nostre classi           |
| `prelabel.py`        | pre-etichetta le registrazioni con un modello                         |
| `train_colab.ipynb`  | addestramento ed esportazione su Google Colab                         |
| `compile.sh`         | compila l'ONNX in `.hef` per l'Hailo                                 |
