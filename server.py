import os
import sys
import jpype
import jpype.imports
from typing import List, Dict, Optional, Any
from fastmcp import FastMCP
import logging

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("jadx-mcp")

# Path to jadx-dev-all.jar - adjust if necessary
JADX_JAR_PATH = "/usr/share/java/jadx-git/lib/jadx-dev-all.jar"

if not os.path.exists(JADX_JAR_PATH):
    logger.error(f"Jadx JAR not found at {JADX_JAR_PATH}")
    # Try to find it in common places if not found
    search_paths = [
        "/usr/local/share/jadx/lib/jadx-dev-all.jar",
        "/opt/jadx/lib/jadx-dev-all.jar",
        os.path.expanduser("~/jadx/lib/jadx-dev-all.jar")
    ]
    for path in search_paths:
        if os.path.exists(path):
            JADX_JAR_PATH = path
            logger.info(f"Found Jadx JAR at {JADX_JAR_PATH}")
            break

# Initialize JVM
try:
    if not jpype.isJVMStarted():
        jpype.startJVM(jpype.getDefaultJVMPath(), classpath=[JADX_JAR_PATH])
    
    # Import Java classes
    from jadx.api import JadxDecompiler, JadxArgs, ResourceType
    from java.io import File, PrintStream, OutputStream
    from java.lang import System
    from java.util import List as JavaList

    # Suppress Jadx/Java stdout/stderr noise to keep MCP protocol clean
    try:
        from java.io import FileOutputStream
        null_out = FileOutputStream("/dev/null")
        null_stream = PrintStream(null_out)
        System.setOut(null_stream)
        System.setErr(null_stream)
    except:
        # Fallback for systems where /dev/null might not be available as a file
        pass

except Exception as e:
    logger.error(f"Failed to initialize JVM: {e}")
    sys.exit(1)

mcp = FastMCP("Jadx MCP Server")

# Global session storage
# Maps session_id -> JadxDecompiler instance
active_sessions: Dict[str, Any] = {}

def get_decompiler(session_id: str) -> JadxDecompiler:
    if session_id not in active_sessions:
        raise ValueError(f"Session {session_id} not found. Call load_apk first.")
    return active_sessions[session_id]

@mcp.tool()
def load_apk(apk_path: str, session_id: str) -> str:
    """
    Load an APK file into a new or existing Jadx session.
    """
    try:
        if not os.path.exists(apk_path):
            return f"Error: APK file not found at {apk_path}"
        
        # Prevent clobbering existing sessions
        if session_id in active_sessions:
            return f"Error: Session ID '{session_id}' is already active. Please use a unique ID or close the existing session first."
            
        args = JadxArgs()
        args.setInputFile(File(apk_path))
        # Headless mode usually doesn't need out dir if we only want code in memory,
        # but some versions might expect it.
        
        decompiler = JadxDecompiler(args)
        decompiler.load()
        
        active_sessions[session_id] = decompiler
        return f"Successfully loaded APK: {apk_path} into session: {session_id}"
    except Exception as e:
        return f"Failed to load APK: {str(e)}"

@mcp.tool()
def close_session(session_id: str) -> str:
    """
    Dispose of a Jadx session and free memory.
    """
    if session_id in active_sessions:
        try:
            decompiler = active_sessions.pop(session_id)
            decompiler.close()
            # Explicitly suggest GC (though not guaranteed in JPype)
            # jpype.java.lang.System.gc() 
            return f"Session {session_id} closed."
        except Exception as e:
            return f"Error closing session {session_id}: {str(e)}"
    return f"Session {session_id} does not exist."

@mcp.tool()
def get_all_classes(session_id: str) -> List[str]:
    """
    Get a list of all class names in the APK.
    """
    try:
        decompiler = get_decompiler(session_id)
        return [str(c.getFullName()) for c in decompiler.getClasses()]
    except Exception as e:
        logger.error(f"get_all_classes error: {e}")
        return [f"Error: {str(e)}"]

@mcp.tool()
def get_class_source(class_name: str, session_id: str) -> str:
    """
    Get the Java source code of a specific class.
    """
    try:
        decompiler = get_decompiler(session_id)
        for cls in decompiler.getClasses():
            if str(cls.getFullName()) == class_name:
                return str(cls.getCode())
        return f"Class {class_name} not found."
    except Exception as e:
        return f"Error: {str(e)}"

@mcp.tool()
def get_methods_of_class(class_name: str, session_id: str) -> List[str]:
    """
    List all method signatures of a class.
    """
    try:
        decompiler = get_decompiler(session_id)
        for cls in decompiler.getClasses():
            if str(cls.getFullName()) == class_name:
                return [str(m.toString()) for m in cls.getMethods()]
        return [f"Class {class_name} not found."]
    except Exception as e:
        return [f"Error: {str(e)}"]

@mcp.tool()
def get_fields_of_class(class_name: str, session_id: str) -> List[str]:
    """
    List all fields of a class.
    """
    try:
        decompiler = get_decompiler(session_id)
        for cls in decompiler.getClasses():
            if str(cls.getFullName()) == class_name:
                return [str(f.toString()) for f in cls.getFields()]
        return [f"Class {class_name} not found."]
    except Exception as e:
        return [f"Error: {str(e)}"]

@mcp.tool()
def get_method_by_name(class_name: str, method_name: str, session_id: str) -> str:
    """
    Get the source code of a specific method.
    """
    try:
        decompiler = get_decompiler(session_id)
        for cls in decompiler.getClasses():
            if str(cls.getFullName()) == class_name:
                # Trigger decompilation to ensure offsets/code are available
                cls.getCode()
                for method in cls.getMethods():
                    if str(method.getName()) == method_name:
                        code = method.getCodeStr()
                        if code:
                            return str(code)
                        else:
                            return "Method found but no code snippet available. Try getting class source."
                return f"Method {method_name} not found in class {class_name}."
        return f"Class {class_name} not found."
    except Exception as e:
        return f"Error: {str(e)}"

@mcp.tool()
def search_classes_by_keyword(keyword: str, session_id: str) -> List[str]:
    """
    Search for classes containing a keyword in their source code.
    """
    try:
        decompiler = get_decompiler(session_id)
        matching_classes = []
        for cls in decompiler.getClasses():
            code = str(cls.getCode())
            if keyword in code:
                matching_classes.append(str(cls.getFullName()))
        return matching_classes
    except Exception as e:
        return [f"Error: {str(e)}"]

@mcp.tool()
def xrefs_to_class(class_name: str, session_id: str) -> List[str]:
    """
    Find where a class is used.
    """
    try:
        decompiler = get_decompiler(session_id)
        for cls in decompiler.getClasses():
            if str(cls.getFullName()) == class_name:
                return [str(node.toString()) for node in cls.getUseIn()]
        return [f"Class {class_name} not found."]
    except Exception as e:
        return [f"Error: {str(e)}"]

@mcp.tool()
def xrefs_to_method(class_name: str, method_name: str, session_id: str) -> List[str]:
    """
    Find where a method is invoked.
    """
    try:
        decompiler = get_decompiler(session_id)
        for cls in decompiler.getClasses():
            if str(cls.getFullName()) == class_name:
                for method in cls.getMethods():
                    if str(method.getName()) == method_name:
                        return [str(node.toString()) for node in method.getUseIn()]
                return [f"Method {method_name} not found in class {class_name}."]
        return [f"Class {class_name} not found."]
    except Exception as e:
        return [f"Error: {str(e)}"]

@mcp.tool()
def get_android_manifest(session_id: str) -> str:
    """
    Retrieve the AndroidManifest.xml.
    """
    try:
        decompiler = get_decompiler(session_id)
        for res in decompiler.getResources():
            if str(res.getOriginalName()) == "AndroidManifest.xml":
                content = res.loadContent()
                if content and content.getText():
                    return str(content.getText().getCodeStr())
        return "AndroidManifest.xml not found."
    except Exception as e:
        return f"Error: {str(e)}"

@mcp.tool()
def get_strings(session_id: str) -> str:
    """
    Retrieve strings.xml content.
    """
    try:
        decompiler = get_decompiler(session_id)
        for res in decompiler.getResources():
            name = str(res.getOriginalName())
            if name.endswith("strings.xml"):
                content = res.loadContent()
                if content and content.getText():
                    return str(content.getText().getCodeStr())
        return "strings.xml not found."
    except Exception as e:
        return f"Error: {str(e)}"

@mcp.tool()
def get_all_resource_file_names(session_id: str) -> List[str]:
    """
    List all resource files in the APK.
    """
    try:
        decompiler = get_decompiler(session_id)
        return [str(res.getOriginalName()) for res in decompiler.getResources()]
    except Exception as e:
        return [f"Error: {str(e)}"]

if __name__ == "__main__":
    import sys
    if "sse" in sys.argv:
        mcp.run(transport="sse")
    else:
        mcp.run()
