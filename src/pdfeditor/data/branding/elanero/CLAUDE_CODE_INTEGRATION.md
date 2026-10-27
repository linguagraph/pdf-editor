# Elanero — интеграция на одобрените асети с Claude Code

Комплект, възстановен на 28 октомври 2026 г. Оригиналните 21 файла са запазени байт по байт, с оригиналните им относителни пътища. Добавените инструкции не променят дизайна. Хранилището на приложението не е предоставено: примерите по-долу са условни и трябва да се съобразят с реалния framework, версия и build pipeline.

## 1. Бърз старт

1. Разархивирайте ZIP в отделна папка. Стартирайте `python VERIFY_ASSETS.py` от нея, ако имате Python 3. Скриптът използва само стандартната библиотека.
2. Поставете целия разархивиран комплект в `assets/branding/elanero/` в съществуващото приложение, или в еквивалентната вече установена директория. Не презаписвайте несвързани файлове.
3. Отворете Claude Code в корена на приложението и поставете готовия prompt в раздел 9. Ако използвате друг път, заменете го в prompt-а и примерите.
4. Прегледайте промените, изградете и стартирайте пакетираното приложение. Само успешно стартиране в development режим не доказва правилно пакетиране.

`ASSET_MANIFEST.json` описва оригиналните файлове и техните SHA-256. `SHA256SUMS.txt` покрива всички файлове от възстановения комплект, освен самия себе си. `VERIFICATION_REPORT.md` описва вече извършените проверки и ограниченията им.

## 2. Точна карта на ресурсите

Всички пътища в таблицата са спрямо корена на комплекта.

| Предназначение | Оригинален файл | Проверени характеристики |
|---|---|---|
| Windows приложение/инсталатор | `icons/platform/elanero.ico` | 16, 24, 32, 48, 64, 128, 256 px |
| macOS bundle | `icons/platform/elanero.icns` | 8 представяния; физически размери до 1024 px |
| Linux и runtime икони | `icons/png/elanero-{size}.png` | 16, 24, 32, 48, 64, 96, 128, 256, 512, 1024 px; RGBA |
| Browser favicon, ако има web frontend | `icons/platform/favicon.ico` | 16, 32, 48 px; не е основният desktop ICO |
| Light splash | `splash/png/elanero-splash-light-1920x1200.png` | 1920 × 1200, RGB |
| Dark splash | `splash/png/elanero-splash-dark-1920x1200.png` | 1920 × 1200, RGB |
| SVG композиции | `splash/svg/elanero-splash-light.svg`, `splash/svg/elanero-splash-dark.svg` | 1920 × 1200; вграден bitmap master |
| Master | `source/elanero-master-1024.png` | 1024 × 1024, RGBA |
| SVG wrapper на master | `source/elanero-master.svg` | Вграден PNG; не е лого от векторни контури |
| Хоризонтален lockup | `branding/elanero-wordmark.png` | 1200 × 360, RGBA |
| Оригинални бележки | `README.txt` | Запазени без редакция |

Палитрата от оригиналния README е turquoise `#19C4C8`, deep teal `#082F3F`, pale aqua `#DDF6F8`. Това са акцентни цветове, не обещание, че фонът на splash е един плътен цвят.

Не прерисувайте буквата E, не правете нови AI варианти, не сменяйте цветовете, пропорциите, прозрачността, padding-а или имената на оригиналите. Ако build инструментът изисква други имена, създайте идентични копия в build директория и запишете съответствието и хешовете. Не конвертирайте ICO/ICNS повторно без техническа необходимост.

## 3. Първо установете реалната архитектура

Claude Code трябва да прочете repository инструкциите и да намери:

- manifest и lockfile; реалните версии на framework и packager;
- entry point, създаване на прозорците, startup/init lifecycle и theme preference;
- съществуващите resource директории, icon/splash references, installer config и CI workflows;
- идентификаторите на приложението и съществуващите подписи, entitlements и file associations.

Не сменяйте framework или packager заради тази задача. Не измисляйте `appId`, bundle identifier, executable name или install path. Запазете съществуващите идентификатори: промяната им може да наруши updates, shortcut grouping и асоциациите на файлове. Не добавяйте поддръжка за неподдържана платформа само заради наличен ресурс.

Направете карта „source asset → build setting → packaged location → runtime consumer“ за всяка поддържана платформа. Разграничете иконата в executable/bundle/installer от иконата на отворения прозорец. Едната настройка невинаги управлява и двете.

## 4. Platform и build configuration

### Windows ICO

Насочете настройките за executable и installer към `elanero.ico`. Свържете и runtime/window icon, ако framework го изисква отделно. Запазете текущата Windows application identity и shortcut настройки. Проверете новоинсталирано копие, Start menu, taskbar и Alt+Tab. Ако shell показва старо изображение, първо проверете самия нов executable и shortcut target; кеширана икона не доказва грешен ресурс. Не изтривайте системни кешове автоматично.

### macOS ICNS

Включете `elanero.icns` в ресурсите на `.app` и го задайте чрез използвания packager/bundle механизъм. Проверете генерираните icon references в `Info.plist`, Finder и Dock. Подпишете/нотариализирайте чрез съществуващия pipeline след пакетиране; не редактирайте вече подписания `.app`. Не използвайте Windows ICO като macOS bundle icon.

### Linux PNG

Използвайте наличните PNG, обикновено 512 px за основен runtime ресурс и съответните размери за пакета. При ръчно desktop пакетиране копирайте подходящите размери в `share/icons/hicolor/<size>x<size>/apps/<existing-icon-name>.png`. Съгласувайте `Icon=` в desktop entry с това име, без разширение, и запазете съществуващата application identity/WM class. Ако packager генерира тези файлове, настройте него вместо паралелна ръчна инсталация. Проверете case-sensitive пътищата в чист Linux build.

### Условен пример: Electron + electron-builder

Прилагайте само ако проектът вече използва тази комбинация. Слейте следните полета с реалната конфигурация; не заменяйте съществуващи `files`, targets или `extraResources`. Пътищата тук са спрямо project root. Проверете схемата на инсталираната версия.

Първо създайте build копия на PNG иконите с имена `16x16.png`, `24x24.png`, …, `1024x1024.png` в `build/elanero-linux-icons/`. Всяко копие трябва да има същия SHA-256 като съответния `elanero-<size>.png`. Това избягва зависимост от разпознаването на нестандартни имена от packager-а.

```json
{
  "build": {
    "win": { "icon": "assets/branding/elanero/icons/platform/elanero.ico" },
    "mac": { "icon": "assets/branding/elanero/icons/platform/elanero.icns" },
    "linux": { "icon": "build/elanero-linux-icons" },
    "extraResources": [
      {
        "from": "assets/branding/elanero/icons",
        "to": "branding/elanero/icons",
        "filter": ["**/*"]
      },
      {
        "from": "assets/branding/elanero/splash/png",
        "to": "branding/elanero/splash/png",
        "filter": ["*.png"]
      }
    ]
  }
}
```

Минимален main-process resolver за тази конкретна resource схема:

```js
const path = require('node:path');
const { app } = require('electron');

function brandingPath(...parts) {
  const root = app.isPackaged
    ? path.join(process.resourcesPath, 'branding', 'elanero')
    : path.join(app.getAppPath(), 'assets', 'branding', 'elanero');
  return path.join(root, ...parts);
}

// При създаване на BrowserWindow след app ready:
const runtimeWindowIcon = process.platform === 'win32'
  ? brandingPath('icons', 'platform', 'elanero.ico')
  : brandingPath('icons', 'png', 'elanero-512.png');
// Използвайте runtimeWindowIcon за Windows/Linux window icon.
// macOS използва bundle ICNS за основната application икона.
```

Ако проектът ползва отделна app директория, променете development клона според реалния layout. Не използвайте текущата работна директория (`cwd`) като предположение за packaged resources. Не включвайте Node integration в renderer заради четенето на изображения. Използвайте установения безопасен механизъм за локални URL/resource references; при нужда превръщайте абсолютен filesystem path чрез `pathToFileURL`, вместо ръчно да строите `file://` URL.

При Electron Forge, Tauri, Qt, .NET/Avalonia или друг stack използвайте собствената му resource/bundle система и схемата на инсталираната версия. Не поставяйте горния JSON в чужд manifest. Свържете същите реални ICO/ICNS/PNG и splash пътища към съществуващите platform настройки и потвърдете резултата в артефакта.

## 5. Splash, тема и lifecycle

Изберете light/dark според запазеното потребителско предпочитание; ако то е „system“, използвайте системната тема. Определете темата преди първото показване, за да няма светло премигване при dark mode. При липсващо предпочитание следвайте съществуващото поведение на приложението.

За вярно възпроизвеждане използвайте PNG вариантите. SVG композициите съдържат текстови елементи; при липсващи шрифтове визуализацията им може да се различава. Вграденият bitmap master е идентичен във всички три SVG файла, но самият SVG не превръща логото в истински вектор.

Показвайте splash локално, без мрежова заявка. Затворете го, когато основният прозорец е визуално готов И необходимата инициализация е приключила. Не симулирайте готовност с фиксирано изчакване. При startup failure затворете/заменете splash с разбираема грешка и възможност за изход или повторен опит; никога не оставяйте безкрайно блокиращ прозорец. Не бавете startup само за да се вижда брандингът.

Изображенията вече съдържат статична лента и „Loading...“. Не добавяйте втора лента върху тях и не представяйте нарисувания процент като реален progress. За този комплект оставете статичния splash непроменен. Ако продуктът по-късно изисква жив progress, използвайте отделна UI композиция като изрично одобрена производна версия, без редакция на оригиналите.

## 6. Scaling и качество

- Splash е 16:10. Примерен начален logical размер е 960 × 600; намалете го според работната област на малък екран. Не насилвайте 1920 × 1200 logical прозорец.
- Запазете aspect ratio. При други пропорции използвайте contain/letterboxing с подходящ тематичен фон. Не разтягайте и не изрязвайте логото с cover.
- При web renderer задайте на самото изображение `width: 100%; height: 100%; object-fit: contain; display: block;`. Контейнерът трябва да има определени размери. При native toolkit използвайте еквивалентно proportional fitting.
- За икона изберете физически размер поне logical px × device pixel ratio. Например 32 logical px при 200% изисква 64 px. Оставете OS да избира представянията в ICO/ICNS.
- PNG master е 1024 px. Не обещавайте безкрайно остро увеличение на SVG wrapper. При 960 × 600 logical splash и 200% наличният PNG покрива точно 1920 × 1200 физически px.
- Проверете 100%, 125%, 150%, 200%, няколко монитора и light/dark. Полупрозрачните краища и съществуващият padding се запазват. Не прилагайте CSS tint/filter или автоматичен monochrome режим към основната икона.
- Wordmark има пропорция 10:3. Използвайте пропорционално оразмеряване; не отделяйте или пренареждайте елементите.

## 7. Licensing / attribution и произход

В оригиналния комплект има README, но няма отделен LICENSE, удостоверени font licenses или условия за задължителен attribution. Този възстановен пакет не добавя лиценз и не удостоверява права върху марката. Не обозначавайте асетите като MIT, CC0, public domain или като third-party open-source ресурс без основание.

Запазете README и provenance информацията вътре в repository. За публикуване приложете действително потвърдените условия на притежателя и проектните правила за notices; не измисляйте автор, copyright година или credit към услуга. Запишете статуса като „license/attribution not supplied in source pack“ в integration report, докато собственикът не уточни условията. Не добавяйте произволен credit върху splash.

README посочва concept board `a_clean_modern_high_resolution_branding_mockup_a.png` като източник. Този concept board не е включен в намерения ZIP. SVG splash съдържа текст, затова преди избор на SVG renderer проверете използваните font-family и наличните шрифтове. При PNG не е нужен runtime font за изпечения текст, но това само по себе си не установява права върху изходния дизайн. Ако добавяте font файлове към приложението, документирайте отделно техния произход и разрешения.

## 8. Verify — критерии за приключване

1. Проверете оригиналните 21 SHA-256 спрямо `ASSET_MANIFEST.json` преди и след интеграцията. Build копията трябва да съвпадат с изходните файлове.
2. Изпълнете съществуващите релевантни lint/typecheck/tests и production build команди според lockfile и CI. Не надграждайте зависимости като странична промяна.
3. Огледайте съдържанието на пакета: правилни icon references, двата splash PNG, валидни resource пътища и нужните runtime икони. Няма абсолютни developer-machine пътища или нужда от достъп до оригиналния ZIP.
4. Стартирайте инсталираното/пакетираното приложение от друго cwd и без мрежа. Проверете студен старт, normal ready, бавна инициализация и failure. Няма missing asset, double splash, висящ прозорец или грешна тема.
5. На Windows проверете EXE, installer, shortcut и taskbar; на macOS `.app`, Finder/Dock и pipeline подписите; на Linux package icons, desktop entry и runtime window. Проверявайте само действително поддържаните цели.
6. Направете screenshots на light/dark и основните DPI настройки. Сравнете E, цветовете и пропорциите с оригиналите. Проверете малките икони при реалния им размер.
7. Добавете кратък integration report с променените файлове, resource mapping, изпълнените точни команди, резултатите и платформите, които не са тествани. Ако липсва macOS/Linux среда, маркирайте ги като „not tested“, а не като passed.

Самият възстановен ZIP е проверен за целостта на файловете. Приложението и неговият build не са предоставени и не са тествани в тази задача.

## 9. Готов prompt за Claude Code

Копирайте текста между оградите в Claude Code, отворен в repository root. Променете само пътя, ако сте поставили комплекта другаде.

```text
Интегрирай одобрения Elanero branding pack от assets/branding/elanero/ в това съществуващо приложение.

Прочети repository инструкциите и CLAUDE_CODE_INTEGRATION.md в комплекта. Открий реалния stack, версии, resource pipeline, startup lifecycle и supported build targets. Не сменяй framework/packager, application identifiers, signing, file associations или update configuration.

Провери SHA-256 на 21-те оригинални файла срещу ASSET_MANIFEST.json. Запази ги байт по байт, включително README. Не генерирай, прерисувай, преоцветявай, изрязвай или оптимизирай изображенията. Ако build изисква други имена, използвай идентични копия в build директория и запиши mapping/hashes.

Използвай icons/platform/elanero.ico за Windows executable и installer, icons/platform/elanero.icns за macOS bundle, и наличните icons/png/elanero-*.png за Linux и runtime. Свържи отделно window/taskbar icon, когато stack го изисква. favicon.ico е само за web favicon, ако е приложимо. Запази съществуващата application identity.

Включи splash/png/elanero-splash-light-1920x1200.png и elanero-splash-dark-1920x1200.png в production resources. Разрешавай пътищата правилно и в development, и в инсталиран build, независимо от cwd и без мрежа. Избирай user theme преди първия paint; за system preference използвай OS theme. Запази aspect ratio 16:10, contain fitting и DPI-aware размер. Не добавяй втора progress лента: оригиналният splash вече има статична. Затваряй splash при реална готовност на UI и startup init; обработи грешка, отказ и затваряне без висящ прозорец. Не добавяй изкуствено забавяне.

SVG master и splash SVG съдържат bitmap; не ги третирай като path-based vector artwork. Предпочети PNG splash за точната одобрена визия. Не добавяй измислен лиценз или attribution: оригиналът не предоставя такива; запиши този статус и запази provenance/README.

Направи минимални промени в съществуващите build/resource настройки. Ако е Electron + electron-builder, използвай условния пример от ръководството само след проверка спрямо инсталираната версия. За друг stack използвай неговия текущ механизъм. Не презаписвай цели config файлове и не надграждай зависимости.

След това изпълни релевантните съществуващи checks и production builds. Провери ресурсите вътре в пакетите и стартиране от различно cwd, offline, light/dark, 100/125/150/200% scaling, нормален/бавен/неуспешен startup. Провери icon surfaces на достъпните supported OS. Не твърди, че недостъпни платформи са тествани.

Създай integration report: открит stack/версии, списък на променените файлове, source-to-package-to-runtime mapping, точни команди и резултати, screenshots ако средата позволява, непроменени SHA-256 и оставащи ограничения. Покажи reviewable diff. Не публикувай release и не променяй несвързана функционалност.
```

## 10. Официални технически справки

Примерите са сверени на 28 октомври 2026 г.; версията, заключена в приложението, има предимство пред latest документация.

- [Electron BrowserWindow: icon и показване на прозорец](https://www.electronjs.org/docs/latest/api/browser-window)
- [electron-builder configuration](https://www.electron.build/configuration/)
- [electron-builder FileSet / extraResources — official source](https://github.com/electron-userland/electron-builder/blob/master/packages/app-builder-lib/src/options/PlatformSpecificBuildOptions.ts)
- [electron-builder Linux icon input — official source](https://github.com/electron-userland/electron-builder/blob/master/packages/app-builder-lib/src/options/linuxOptions.ts)
