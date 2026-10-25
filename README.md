# Inventory bot
Gestiona a través de Telegram un inventario. Ideal para saber qué tienes en las cajas de tu estudio sin volverte loco.

## Configuracion
Tras copiar y pegar los archivos en tu servidor:
- Habla con @BotFather en Telegram, crea un bot con ```/newbot``` y copia el token.
- Averigua tu ID numérico de Telegram con @userinfobot.
- Rellena ambas informaciones en el archivo ```.env ```
- Ejecuta ``` docker compose up -d --build ```

## Lista de comandos
- ```/add``` nombre | cantidad | lugar | categoría : ```/add``` Destornillador Phillips | 3 | Cajón 2 | Herramientas
<br><sub> Cantidad y categoría son opcionales:
   ```/add``` Taladro | Armario
   ```/add``` Taladro | Armario | Herramientas </sub>
- ```/buscar``` texto – dónde está algo
- ```/lugar``` texto – qué hay en un lugar
- ```/categoria``` texto – qué hay en una categoría
- ```/lugares``` – lugares y nº de objetos
- ```/categorias``` – categorías y nº de objetos
- ```/lista``` – todo el inventario
- ```/cantidad``` id n – fijar cantidad
- ```/mover``` id | nuevo lugar
- ```/setcat``` id | nueva categoría
- ```/borrar``` id
