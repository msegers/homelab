# Terraform PI

This is a terraform config for deploying a k3s cluster with some apps to my raspberry at home.
Feel free to take inspiration, call me out for stupid decisions etc.

I want to be very clear that I copied a lot from [Saura Sahu's homelab project](https://github.com/mrsauravsahu/kube-homelab).

## How to use

Right now in this project I hard coded my local Raspberry IP,
you can update this in `k3s/terraform.tfvars` it would probably be better to have this in some non versioned config,
but to me the data is not sensitive.

First, you should install the latest raspberry OS on your raspy berry, I went with the light 64 bit version.
Make sure your raspberry is connected to the internet, and has SSH enabled,
nowadays you can configure most of this in the raspberry patcher/imager,
I had some issues with getting wifi to run due to country settings not being applied, no idea if that was my bad.
once it's up and running, make sure to set the IP in the vars mentioned above.

Next step, ensure you can freely SSH into it as root (this should likely be changed, seems sus),
you can do this by copying your ssh pub keys into `/root/.ssh/authorized_keys`.

Once you have this all setup you should be able to move into the k3s folder `cd k3s` and run `terraform init` and `terraform apply`.
There's some other ways to do this,
you can also check what's happening with `terraform plan` etc,
but I would suggest you to read up on how terraform works.

If all goes well, you should now have k3s installed on your Raspi, yay.

Now at the current state, the next step is awkward, as you'd need to have my blog checked out,
currently it references to some files in the repo. 
So you might want to comment out the `mathijs.se` related sections in `apps/apps.tf`
To then run this terraform conf, you need to do one thing, you need kubectl access to k3s on pi.
For that you can copy (from the pi) `/etc/rancher/k3s/k3s.config` to your host machines `~/.kube/config`.
Perhaps there's better locations, leave an issue!

Now similar to k3s, you can simply run `terraform init` and `terraform apply` in `apps`, currently there's not much there,
if you disabled my blog (you better), there should just be `pihole` there. 
So unless you just want to copy my things and build on top of it, there are likely better ways to handle this.

