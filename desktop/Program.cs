// Moje JIPka – desktopová aplikace: portál ve vlastním okně s jádrem Edge (WebView2).
// Server se nemění, aplikace jen zobrazuje portál jako prohlížeč (stahování, tisk, PDF,
// pravé tlačítko, F5 – výchozí chování WebView2). Sestavení i instalátor: build.cmd.
// C# 5 záměrně: kompiluje ho csc.exe, který je součástí Windows (.NET Framework 4.8).
using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Runtime.InteropServices;
using System.Windows.Forms;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

static class Program
{
    // Ostrý portál. Jednorázově jinam (např. test server): JIPka.exe http://127.0.0.1:8080/
    const string VychoziUrl = "https://analytikasys.jip-napoje.cz/";

    // ESC → dotaz na ukončení, pokud klávesu nespotřebovala stránka (preventDefault – Směny,
    // kontextové menu Sankcí). Dialogy a menu Quasaru se zavírají až na keyup a editace buňky
    // AG Gridu preventDefault nevolá, proto se hlídají v DOM ještě před obsluhami stránky.
    const string EscSkript = @"addEventListener('keydown', function (e) {
    if (e.key !== 'Escape' || e.repeat || window.top !== window
        || document.querySelector('.q-dialog, .q-menu, .ag-popup, .ag-cell-inline-editing')) return;
    setTimeout(function () { if (!e.defaultPrevented) chrome.webview.postMessage('esc'); });
}, true);";

    [DllImport("user32.dll")]
    static extern bool SetProcessDpiAwarenessContext(IntPtr hodnota);

    [STAThread]
    static void Main(string[] args)
    {
        // Per-Monitor V2 – jinak Windows při škálování 125 % a víc celé okno rozmaže.
        SetProcessDpiAwarenessContext(new IntPtr(-4));
        Application.EnableVisualStyles();

        var adresa = new Uri(args.Length > 0 ? args[0] : VychoziUrl);
        var okno = new Form
        {
            Text = "Moje JIPka",
            Size = new Size(1280, 800),
            WindowState = FormWindowState.Maximized
        };
        using (var ikona = typeof(Program).Assembly.GetManifestResourceStream("jipka.ico"))
            okno.Icon = new Icon(ikona);
        var web = new WebView2 { Dock = DockStyle.Fill };
        okno.Controls.Add(web);

        // F11 = celá obrazovka (bez rámečku, přes hlavní panel), další F11 = zpět.
        var predtim = FormWindowState.Maximized;
        web.KeyDown += (s, e) =>
        {
            if (e.KeyCode != Keys.F11) return;
            e.Handled = true;
            var naCelou = okno.FormBorderStyle != FormBorderStyle.None;
            if (naCelou) predtim = okno.WindowState;
            okno.FormBorderStyle = naCelou ? FormBorderStyle.None : FormBorderStyle.Sizable;
            okno.WindowState = FormWindowState.Normal;  // přes Normal, jinak Windows maximalizaci nepřepočte
            okno.WindowState = naCelou ? FormWindowState.Maximized : predtim;
        };

        okno.Load += async (s, e) =>
        {
            try
            {
                // Vlastní profil v %LOCALAPPDATA%\JIPka – přihlášení vydrží mezi spuštěními.
                // Přihlášení Microsoft účtem projde samo účtem Windows, jako v Edge.
                var profil = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "JIPka");
                var prostredi = await CoreWebView2Environment.CreateAsync(null, profil,
                    new CoreWebView2EnvironmentOptions { AllowSingleSignOnUsingOSPrimaryAccount = true });
                await web.EnsureCoreWebView2Async(prostredi);
            }
            catch (WebView2RuntimeNotFoundException)
            {
                if (MessageBox.Show("Chybí Microsoft Edge WebView2 Runtime. Otevřít stránku ke stažení?", "Moje JIPka",
                        MessageBoxButtons.YesNo, MessageBoxIcon.Warning) == DialogResult.Yes)
                    Process.Start("https://go.microsoft.com/fwlink/p/?LinkId=2124703");
                okno.Close();
                return;
            }
            var jadro = web.CoreWebView2;
            // Podle této značky portál v aplikaci nenabízí stažení aplikace (intranet.py).
            jadro.Settings.UserAgent += " JIPkaDesktop";
            jadro.DocumentTitleChanged += (s2, e2) => okno.Text = jadro.DocumentTitle;
            // Nová okna portálu (přílohy, tiskové sestavy z blob:) zůstávají v aplikaci se stejným
            // přihlášením (výchozí okno WebView2); cizí weby jdou do výchozího prohlížeče.
            jadro.NewWindowRequested += (s2, e2) =>
            {
                Uri cil;
                if (Uri.TryCreate(e2.Uri, UriKind.Absolute, out cil)
                    && (cil.Scheme == Uri.UriSchemeHttp || cil.Scheme == Uri.UriSchemeHttps)
                    && cil.Host != adresa.Host)
                {
                    e2.Handled = true;
                    Process.Start(e2.Uri);
                }
            };
            await jadro.AddScriptToExecuteOnDocumentCreatedAsync(EscSkript);
            var ptaSe = false;
            jadro.WebMessageReceived += (s2, e2) =>
            {
                // Modální dotaz pumpuje zprávy – bez pojistky by rychlý druhý ESC otevřel druhý dotaz.
                if (ptaSe || e2.WebMessageAsJson != "\"esc\"") return;
                ptaSe = true;
                var konec = MessageBox.Show(okno, "Ukončit aplikaci Moje JIPka?", "Moje JIPka",
                    MessageBoxButtons.YesNo, MessageBoxIcon.Question) == DialogResult.Yes;
                ptaSe = false;
                // Zavřít až po návratu z události WebView2 (zavření okna ho ruší).
                if (konec) okno.BeginInvoke(new Action(okno.Close));
            };
            web.Source = adresa;
        };
        Application.Run(okno);
    }
}
