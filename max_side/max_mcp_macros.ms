-- max_mcp_macros.ms -- macroScripts for max-mcp (category "max-mcp").
-- After this file ran once, the actions show up in Customize > Customize User
-- Interface (category max-mcp) and can be put on any menu, toolbar or quad
-- menu. max_mcp_start.ms loads this file; it is also safe to run on its own.
-- The ribbon editor (right-click the ribbon > Customize Ribbon) lists them too.
--
-- Icons: max_side/icons/MaxMcp/*.svg are copied to #userIcons/MaxMcp (one of
-- the folders iconName is resolved from) before the macroScripts are defined,
-- and only when missing or changed.

global MaxMcp_Running

(
    local src = ""
    try ( src = (getFilenamePath (getSourceFileName())) + "icons\\MaxMcp\\" ) catch ()
    local dst = (getDir #userIcons) + "\\MaxMcp\\"
    if src != "" do (
        makeDir dst all:true
        local ioFile = dotNetClass "System.IO.File"
        for f in getFiles (src + "*.svg") do (
            local t = dst + (filenameFromPath f)
            local same = false
            try ( same = (doesFileExist t) and (ioFile.ReadAllText f) == (ioFile.ReadAllText t) ) catch ()
            if not same do (
                try ( ioFile.Copy f t true ) catch ( format "[MaxMcp] icon copy failed: %\n" (getCurrentException()) )
            )
        )
    )
)

macroScript MaxMcp_Settings
    category:"max-mcp"
    buttonText:"MaxMcp Settings"
    toolTip:"max-mcp: listener settings, start/stop, client configuration"
    iconName:"MaxMcp/Settings"
(
    on execute do (
        try ( python.Execute "import max_mcp_ui; max_mcp_ui.show()" )
        catch ( messageBox ("max-mcp settings window failed:\n" + (getCurrentException())) title:"max-mcp" )
    )
)

macroScript MaxMcp_Toggle
    category:"max-mcp"
    buttonText:"MaxMcp Start/Stop"
    toolTip:"max-mcp: start or stop the listener"
    iconName:"MaxMcp/Toggle"
(
    -- Python -> MAXScript sonuc aktarimi global degisken uzerinden (pymxs.runtime.<global>)
    on isChecked do (
        try ( python.Execute "import max_mcp_listener, pymxs; pymxs.runtime.MaxMcp_Running = max_mcp_listener.is_running()"; MaxMcp_Running == true )
        catch ( false )
    )
    on execute do (
        try (
            python.Execute "import max_mcp_listener, pymxs; pymxs.runtime.MaxMcp_Running = max_mcp_listener.is_running()"
            if MaxMcp_Running == true then ( python.Execute "max_mcp_listener.stop()"; format "[MaxMcp] stopped\n" )
            else ( python.Execute "max_mcp_listener.start()"; format "[MaxMcp] started\n" )
        ) catch ( messageBox ("max-mcp toggle failed:\n" + (getCurrentException())) title:"max-mcp" )
    )
)
