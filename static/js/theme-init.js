(function () {
    try {
        var t = localStorage.getItem("theme") || "auto";
        var d = window.matchMedia("(prefers-color-scheme: dark)").matches;
        if (t === "dark" || (t === "auto" && d)) {
            document.documentElement.classList.add("dark");
        } else {
            document.documentElement.classList.remove("dark");
        }
    } catch (e) {}
})();
