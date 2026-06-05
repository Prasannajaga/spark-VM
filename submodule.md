## Managing `cracker-sdk` as a Git Submodule

You can add `cracker-sdk` inside your project as a Git submodule.
This allows your main project to track a specific version of `cracker-sdk`.

---

## Add `cracker-sdk` as a submodule

```bash
git submodule add https://github.com/Prasannajaga/cracker-sdk external/cracker-sdk
```

```bash
# This adds the cracker-sdk repository inside:
# external/cracker-sdk
#
# Git will also create or update the .gitmodules file.
# Your main repo will only track the submodule commit pointer,
# not copy the full cracker-sdk source code directly.
```

---

## Commit the new submodule

```bash
git add .gitmodules external/cracker-sdk
git commit -m "Add cracker-sdk as submodule"
```

```bash
# This commits the submodule configuration.
#
# .gitmodules stores the submodule URL and path.
# external/cracker-sdk stores the exact cracker-sdk commit
# that your main project should use.
```

---

## Clone a project with submodules

```bash
git clone --recurse-submodules <your-repo-url>
```

```bash
# This clones your main repository and also downloads
# all configured submodules automatically.
#
# Use this when cloning the project for the first time.
```

---

## Initialize submodules after cloning

```bash
git submodule update --init --recursive
```

```bash
# Use this if you already cloned the main repository
# but the submodule folder is empty.
#
# --init initializes the submodule.
# --recursive also initializes nested submodules, if any exist.
```

---

## Check submodule status

```bash
git submodule status
```

```bash
# This shows the current commit checked out
# for each submodule in your project.
```

Example output:

```bash
abc123 external/cracker-sdk
```

```bash
# This means external/cracker-sdk is currently pointing
# to commit abc123.
```

---

## Update the submodule to the latest version

```bash
cd external/cracker-sdk
git checkout main
git pull origin main
cd ../..
```

```bash
# This enters the cracker-sdk submodule,
# switches to the main branch,
# pulls the latest changes from GitHub,
# then returns back to your main project.
```

---

## Commit the updated submodule pointer

```bash
git add external/cracker-sdk
git commit -m "Update cracker-sdk submodule"
```

```bash
# This does not commit the full cracker-sdk code.
#
# It only updates the commit pointer stored in your main repo.
# This tells Git which exact cracker-sdk version your project should use.
```

Example:

```bash
# Before update:
# external/cracker-sdk -> abc123
#
# After update:
# external/cracker-sdk -> def456
#
# Your commit stores the change from abc123 to def456.
```

---

## Update all submodules

```bash
git submodule update --remote --merge
```

```bash
# This updates all submodules to the latest commit
# from their configured remote tracking branch.
```

Then commit the updated pointers:

```bash
git add .
git commit -m "Update submodules"
```

```bash
# This saves the new submodule commit versions
# in your main repository.
```

---

## Pull latest main repo changes and sync submodules

```bash
git pull
git submodule update --init --recursive
```

```bash
# git pull updates your main repository.
# git submodule update makes sure your submodules
# match the exact commits expected by the main repo.
```

---

## Make changes inside the submodule

```bash
cd external/cracker-sdk
git checkout -b my-change
```

```bash
# This enters the cracker-sdk submodule
# and creates a new branch for your changes.
```

After editing files:

```bash
git add .
git commit -m "Improve cracker-sdk"
git push origin my-change
```

```bash
# This commits and pushes your changes
# to the cracker-sdk repository itself.
```

Then return to the main project:

```bash
cd ../..
git add external/cracker-sdk
git commit -m "Update cracker-sdk submodule pointer"
```

```bash
# This updates your main project to point
# to the new cracker-sdk commit you just created.
```

---

## Show submodule changes

```bash
git diff --submodule
```

```bash
# This shows which submodule commit changed.
# Useful before committing a submodule update.
```

---

## Remove the submodule

```bash
git submodule deinit -f external/cracker-sdk
git rm -f external/cracker-sdk
rm -rf .git/modules/external/cracker-sdk
git commit -m "Remove cracker-sdk submodule"
```

```bash
# This removes the cracker-sdk submodule from your project.
#
# git submodule deinit disables the submodule.
# git rm removes it from Git tracking.
# rm -rf removes the internal Git metadata for the submodule.
# git commit saves the removal.
```
