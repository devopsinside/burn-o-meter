class BurnOMeter < Formula
  include Language::Python::Virtualenv

  desc "See what your AI coding agents really cost, without sending your data anywhere"
  homepage "https://github.com/devopsinside/burn-o-meter"
  url "https://github.com/devopsinside/burn-o-meter/releases/download/v0.3.3/burn_o_meter-0.3.3.tar.gz"
  sha256 "4974c4ac9fff37b9d61b433be1f0d91260302d4e2de7a815cd4e4617912b4f61"
  license "MIT"


  depends_on "python@3.14"

  resource "markdown-it-py" do
    url "https://files.pythonhosted.org/packages/06/ff/7841249c247aa650a76b9ee4bbaeae59370dc8bfd2f6c01f3630c35eb134/markdown_it_py-4.2.0.tar.gz"
    sha256 "04a21681d6fbb623de53f6f364d352309d4094dd4194040a10fd51833e418d49"
  end

  resource "mdurl" do
    url "https://files.pythonhosted.org/packages/d6/54/cfe61301667036ec958cb99bd3efefba235e65cdeb9c84d24a8293ba1d90/mdurl-0.1.2.tar.gz"
    sha256 "bb413d29f5eea38f31dd4754dd7377d4465116fb207585f97bf925588687c1ba"
  end

  resource "pygments" do
    url "https://files.pythonhosted.org/packages/49/2e/ced460408999b33da6b31b0021b0f37d329e202d4169aeb164493778f25b/pygments-2.21.0.tar.gz"
    sha256 "610ca751c9bc2492b38eb9a38a7fbc93edbbb2d7182edaf34e66ae493dee5c8c"
  end

  resource "rich" do
    url "https://files.pythonhosted.org/packages/c0/8f/0722ca900cc807c13a6a0c696dacf35430f72e0ec571c4275d2371fca3e9/rich-15.0.0.tar.gz"
    sha256 "edd07a4824c6b40189fb7ac9bc4c52536e9780fbbfbddf6f1e2502c31b068c36"
  end

  def install
    virtualenv_install_with_resources
  end

  # Keeps the numbers fresh in the background. Deliberately gentle: a scan is
  # incremental, so a steady-state tick reads a few KB and exits in milliseconds.
  service do
    run [opt_bin/"burnometer", "scan", "--quiet"]
    run_type :interval
    interval 60
    process_type :background
    log_path var/"log/burn-o-meter.log"
    error_log_path var/"log/burn-o-meter.log"
  end

  def caveats
    <<~EOS
      burn-o-meter reads your agent logs from disk and never sends anything
      anywhere. Get started with:
        burn-o-meter scan && burn-o-meter today

      To keep it up to date in the background, pick ONE of these -- running both
      schedules two scans on the same interval, which wastes power for no gain:
        brew services start burn-o-meter   # the Homebrew way
        burn-o-meter agent install         # the built-in way

      This formula installs the command line tool only -- nothing appears in your
      menu bar. The app is a separate, optional build, because an unsigned app
      cannot ship through Homebrew without a Gatekeeper warning:
        git clone https://github.com/devopsinside/burn-o-meter
        cd burn-o-meter && macos/make-app.sh --install
        open /Applications/burn-o-meter.app

      --install matters: macOS will not register a login item for an app outside
      /Applications, so one built in place never comes back after a reboot.
    EOS
  end

  test do
    assert_match version.to_s, shell_output("#{bin}/burn-o-meter --version")
  end
end
