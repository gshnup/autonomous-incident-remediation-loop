Vagrant.configure("2") do |config|

  # =========================
  # CONTROL SERVER
  # =========================
  config.vm.define "control" do |control|

    control.vm.box = "ubuntu/jammy64"

    control.vm.hostname = "control"

    control.vm.network "private_network", ip: "192.168.56.10"

    control.vm.provider "virtualbox" do |vb|
      vb.memory = 2048
      vb.cpus = 2
    end

  end

  # =========================
  # WEB SERVER
  # =========================
  config.vm.define "web" do |web|

    web.vm.box = "ubuntu/jammy64"

    web.vm.hostname = "web"

    web.vm.network "private_network", ip: "192.168.56.11"

    web.vm.provider "virtualbox" do |vb|
      vb.memory = 1024
      vb.cpus = 1
    end

  end

  # =========================
  # DATA SERVER
  # =========================
  config.vm.define "data" do |data|

    data.vm.box = "ubuntu/jammy64"

    data.vm.hostname = "data"

    data.vm.network "private_network", ip: "192.168.56.12"

    data.vm.provider "virtualbox" do |vb|
      vb.memory = 1024
      vb.cpus = 1
    end

  end

end