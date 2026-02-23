/**
 * @smilintux/skcapstone
 *
 * SKCapstone - The sovereign agent framework.
 * This is a JS/TS bridge to the Python skcapstone package.
 * Install the Python package for full functionality: pip install skcapstone
 */

const { execSync } = require("child_process");

const VERSION = "0.1.0";
const PYTHON_PACKAGE = "skcapstone";

function checkInstalled() {
  try {
    execSync(`python3 -c "import skcapstone"`, { stdio: "pipe" });
    return true;
  } catch {
    return false;
  }
}

function run(args) {
  return execSync(`skcapstone ${args}`, { encoding: "utf-8" });
}

module.exports = {
  VERSION,
  PYTHON_PACKAGE,
  checkInstalled,
  run,
};
