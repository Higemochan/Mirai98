'use strict';

// A small menu.  The page has its own navigation, so this is only for the
// things a window is expected to do.

const { Menu, shell, app } = require('electron');

function installMenu() {
  const template = [
    {
      label: 'File',
      submenu: [{ role: 'quit' }],
    },
    {
      label: 'View',
      submenu: [
        { role: 'reload' },
        { role: 'resetZoom' },
        { role: 'zoomIn' },
        { role: 'zoomOut' },
        { type: 'separator' },
        { role: 'togglefullscreen' },
        { role: 'toggleDevTools' },
      ],
    },
    {
      label: 'Help',
      submenu: [
        {
          label: 'Where the machines are kept',
          click: () => shell.openPath(
            process.env.MIRAI98_BASE
            || require('path').join(app.getPath('appData'), 'Mirai98')),
        },
      ],
    },
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

module.exports = { installMenu };
