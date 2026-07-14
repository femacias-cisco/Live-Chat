(function loadEngageWidget() {
    "use strict";

    var mount = document.getElementById("divicw");
    if (!mount || !mount.dataset.bind) {
        console.error("Engage widget mount or binding is missing.");
        return;
    }

    var loader = document.createElement("script");
    loader.src = "https://attachments-ldn.imiengage.io/widgeteu/js/imichatinit.js?t=" + new Date().toISOString();
    loader.addEventListener("load", function () {
        console.log(new Date().toISOString(), "Livechat script loaded successfully!");
    });
    loader.addEventListener("error", function () {
        console.log(new Date().toISOString(), "Error loading Livechat script");
        showUnsupportedBrowserNotice();
    });
    mount.insertAdjacentElement("afterend", loader);

    function showUnsupportedBrowserNotice() {
        var fallback = document.createElement("iframe");
        fallback.id = "tls_al_frm";
        fallback.title = "LiveChat browser support notice";
        fallback.setAttribute("frameborder", "0");
        fallback.style.cssText = "overflow:hidden;height:208px;width:394px;position:fixed;right:48px;bottom:12px;z-index:99999;display:block;max-width:calc(100vw - 24px);";
        document.body.appendChild(fallback);

        var fallbackDocument = fallback.contentWindow.document;
        fallbackDocument.open();
        fallbackDocument.write("<!doctype html><html><head><meta charset='utf-8'><title>LiveChat notice</title><style>body{font-family:'Helvetica Neue',Helvetica,Arial,sans-serif;color:#56627c;font-size:14px}.notice{background:#fbfbfe;padding:1.5rem;border-radius:5px;width:300px;box-shadow:0 2px 5px rgba(0,0,0,.26);position:relative}.notice strong{font-size:16px}.notice button{position:absolute;right:12px;top:10px;border:0;background:transparent;color:#56627c;font-size:18px;cursor:pointer}</style></head><body><div class='notice'><button type='button' aria-label='Close' onclick='window.parent.postMessage({action:\"close_tls_alert\"},\"*\")'>×</button><strong>This browser version is not supported on LiveChat.</strong><p>Please update your browser to the latest version and reopen the website to access the widget.</p></div></body></html>");
        fallbackDocument.close();
    }

    window.addEventListener("message", function (event) {
        if (event.data && event.data.action === "close_tls_alert") {
            var fallback = document.getElementById("tls_al_frm");
            if (fallback) {
                fallback.remove();
            }
        }
    });
}());
