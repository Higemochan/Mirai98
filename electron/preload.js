'use strict';

// Deliberately almost empty.
//
// The page is the same one the appliance serves to an ordinary browser, so
// it must not come to depend on anything Electron gives it.  This file
// exists so that contextIsolation has somewhere to attach, and as the one
// place to put a bridge if the Windows side ever needs one.
