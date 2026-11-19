# Launch animation

`static/splash.js` (+ `static/splash-eye.png`, the eye logo cut from logo-full.png with a transparent background) is loaded first in `index.html` and `login.html`.
Sequence (about 3 s): the eye logo glows in, "TradeLens" rises, "SEE THE MARKET CLEARLY" glows in, then "AI-powered analysis for Deriv Synthetic Indices"; then it fades out onto the page, which has been loading underneath the whole time. Tap anywhere to skip.
Shown once per browser session, which is once each time the app is opened (sessionStorage `tl_splash`), not on every page. Devices set to reduce motion get a short still fade (0.9 s). To change the words, edit the three text lines in `splash.js`; bump `?v=` in the two script tags after changing it. To switch it off, remove the script tag from both pages.
