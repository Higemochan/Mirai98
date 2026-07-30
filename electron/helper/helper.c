/* The face of the elevated half of writing a real drive.
 *
 * All this does is start pythonw.exe on drives/helper.py, from its own
 * directory, and pass its arguments along.  It exists so that the UAC
 * dialog names Mirai98 rather than Python, and so that what gets admin
 * rights is one small program that does one thing.
 *
 * Built with the same mingw toolchain as QEMU:
 *   x86_64-w64-mingw32-gcc -municode -mwindows -O2 helper.c -o mirai98-helper.exe
 */

#include <windows.h>

int WINAPI wWinMain(HINSTANCE hi, HINSTANCE hp, PWSTR args, int show)
{
    WCHAR dir[MAX_PATH];
    GetModuleFileNameW(NULL, dir, MAX_PATH);
    WCHAR *slash = wcsrchr(dir, L'\\');
    if (slash) *slash = 0;

    /* "<dir>\python\pythonw.exe" "<dir>\src\drives\helper.py" <args> */
    WCHAR cmd[3 * MAX_PATH + 64];
    wsprintfW(cmd, L"\"%s\\python\\pythonw.exe\" \"%s\\src\\drives\\helper.py\" %s",
              dir, dir, args);

    STARTUPINFOW si;
    PROCESS_INFORMATION pi;
    ZeroMemory(&si, sizeof si);
    si.cb = sizeof si;
    if (!CreateProcessW(NULL, cmd, NULL, NULL, FALSE, CREATE_NO_WINDOW,
                        NULL, dir, &si, &pi))
        return 111;
    WaitForSingleObject(pi.hProcess, INFINITE);
    DWORD code = 112;
    GetExitCodeProcess(pi.hProcess, &code);
    CloseHandle(pi.hProcess);
    CloseHandle(pi.hThread);
    return (int)code;
}
