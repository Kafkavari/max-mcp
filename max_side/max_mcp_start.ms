-- max_mcp_start.ms -- loads max-mcp inside a running 3ds Max.
--   * puts max_side/ and the repo root on sys.path (repo can live anywhere)
--   * registers the macroScripts (Customize UI > category "max-mcp")
--   * starts the listener: always when run by hand (Scripting > Run Script),
--     and only if the 'autostart' setting is on when run from the startup
--     loader (the loader sets MaxMcp_StartupLoad = true before fileIn).
-- Settings: %LOCALAPPDATA%\max-mcp\config.json via the settings window.
--
-- Main-thread strategy: the Python module installs a PySide6 QTimer (Max's
-- own Qt event loop fires it on the UI thread). If that fails for any reason
-- we fall back to a System.Windows.Forms.Timer that calls pump() -- the same
-- technique that has proven reliable for UI-thread work in MAXScript.

global MaxMcp_Timer
global MaxMcp_Tick
global MaxMcp_StartupLoad
global MaxMcp_Autostart
fn MaxMcp_Tick s e = ( try ( python.Execute "max_mcp_listener.pump()" ) catch ( format "[MaxMcp] pump error: %\n" (getCurrentException()) ) )

fn MaxMcp_StartListener = (
    local okQt = false
    try (
        python.Execute "max_mcp_listener.start(pump='qt')"
        okQt = true
    ) catch (
        format "[MaxMcp] QTimer pump failed (%), using dotNet timer\n" (getCurrentException())
    )
    if not okQt do (
        python.Execute "max_mcp_listener.start(pump='none')"
        if MaxMcp_Timer != undefined do ( try ( MaxMcp_Timer.Stop(); MaxMcp_Timer.Dispose() ) catch () )
        MaxMcp_Timer = dotNetObject "System.Windows.Forms.Timer"
        MaxMcp_Timer.Interval = 50
        dotNet.addEventHandler MaxMcp_Timer "Tick" MaxMcp_Tick
        MaxMcp_Timer.Start()
        format "[MaxMcp] dotNet timer pump active (50 ms)\n"
    )
)

(
    local selfDir = ""
    try ( selfDir = getFilenamePath (getSourceFileName()) ) catch ()
    if selfDir == "" do selfDir = (getDir #userScripts) + "\\max-mcp\\max_side\\"
    local pyDir = substituteString selfDir "\\" "/"
    local repoDir = substituteString (pathConfig.removePathLeaf (trimRight selfDir "\\/")) "\\" "/"
    python.Execute ("import sys\nfor _p in (r'" + pyDir + "', r'" + repoDir + "'):\n    _p not in sys.path and sys.path.insert(0, _p)\nimport importlib, max_mcp_listener\nimportlib.reload(max_mcp_listener)")

    try ( fileIn (selfDir + "max_mcp_macros.ms") ) catch ( format "[MaxMcp] macros failed: %\n" (getCurrentException()) )

    local fromStartup = (MaxMcp_StartupLoad == true)
    MaxMcp_StartupLoad = false
    local autostart = true
    if fromStartup do (
        try (
            python.Execute "import pymxs; pymxs.runtime.MaxMcp_Autostart = bool(max_mcp_listener.current_config()['autostart'])"
            autostart = (MaxMcp_Autostart == true)
        ) catch ( autostart = true )
    )
    if autostart then MaxMcp_StartListener()
    else format "[MaxMcp] loaded, autostart is off (use MaxMcp Settings to start)\n"
)
