#!/bin/bash -e
#
# ./deployandtest.sh [aws|aws-single-node|gcp|metal|openstack|shakenfist]

#### Required settings
VARIABLES=""
VERBOSE="-v"

# Determine what version of Shaken Fist is installed locally, as that
# is what we will use for any remote hosts.
install_path=`python3 -m pip show shakenfist | egrep "^Location: " | cut -f 2 -d " "`
direct_url_path=$install_path/shakenfist-*dist-info/direct_url.json
if [ -e $direct_url_path ]
then
  REMOTE_SOURCE="file"
  REMOTE_PACKAGE=`cat $direct_url_path | python3 -c "import sys, json; print(json.loads(sys.stdin.read())['url']);"`

  if [ `echo "$REMOTE_PACKAGE" | egrep -c "^file://"` -ne 1 ]
  then
    echo "Sorry, $REMOTE_PACKAGE is not a supported package source."
    exit 1
  fi

  REMOTE_PACKAGE=`echo $REMOTE_PACKAGE | sed 's|file://||'`
  echo "Shaken Fist is a locally built package from $REMOTE_PACKAGE"
else
  pip_version=`egrep "^Version :" $install_path/shakenfist-*dist.info/METADATA | cut -f 2 -d " "`
  echo "Shaken Fist is pip version $pip_version"
  REMOTE_SOURCE="pip"
  REMOTE_PACKAGE="shakenfist==$pip_version"
fi

#### AWS
if [ "$CLOUD" == "aws" ] || [ "$CLOUD" == "aws-single-node" ]
then
  if [ -z "$AWS_REGION" ]
  then
    echo ===== Must specify AWS region in \$AWS_REGION
    exit 1
  fi
  VARIABLES="$VARIABLES,region=$AWS_REGION"

  if [ -z "$AWS_AVAILABILITY_ZONE" ]
  then
    echo ===== Must specify AWS availability zone in \$AWS_AVAILABILITY_ZONE
    exit 1
  fi
  VARIABLES="$VARIABLES,availability_zone=$AWS_REGION"

  if [ -z "$AWS_VPC_ID" ]
  then
    echo ===== Must specify AWS VPC ID in \$AWS_VPC_ID
    exit 1
  fi
  VARIABLES="$VARIABLES,vpc_id=$AWS_VPC_ID"

  if [ -z "$AWS_SSH_KEY_NAME" ]
  then
    echo ===== Must specify AWS Instance SSH key name in \$AWS_SSH_KEY_NAME
    exit 1
  fi
  VARIABLES="$VARIABLES,ssh_key_name=$AWS_SSH_KEY_NAME"
fi

#### Google Cloud
if [ "$CLOUD" == "gcp" ]
then
  if [ -z "$GCP_PROJECT" ]
  then
    echo ===== Must specify GCP project in \$GCP_PROJECT
    exit 1
  fi

  if [ -z "$NODE_IMAGE" ]
  then
    echo ===== Must specify the name of a boot image in \$NODE_IMAGE
    exit 1
  fi

  if [ -z "$NODE_COUNT" ]
  then
    echo ===== Must specify the number of nodes to create in \$NODE_COUNT
    exit 1
  fi

  VARIABLES="$VARIABLES,project=$GCP_PROJECT,node_image=$NODE_IMAGE,node_count=$NODE_COUNT"
  IGNORE_MTU="1"

  if [ -n "$GCP_SSH_KEY_FILENAME" ]
  then
    d=`cat $GCP_SSH_KEY_FILENAME.pub`
    VARIABLES="$VARIABLES,ssh_key_filename=$GCP_SSH_KEY_FILENAME"
    VARIABLES="$VARIABLES,ssh_key=\"$d\",ssh_user=$GCP_SSH_USER"
  else
    VARIABLES="$VARIABLES,ssh_key_filename=\"\",ssh_key=\"\",ssh_user=\"\""
  fi
fi

#### Openstack
if [ "$CLOUD" == "openstack" ]
then
  if [ -z "$OS_SSH_KEY_NAME" ]
  then
    echo ===== Must specify Openstack SSH key name in \$OS_SSH_KEY_NAME
    exit 1
  fi
  VARIABLES="$VARIABLES,ssh_key_name=$OS_SSH_KEY_NAME"

  if [ -z "$OS_FLAVOR_NAME" ]
  then
    echo ===== Must specify Openstack instance flavor name in \$OS_FLAVOR_NAME
    exit 1
  fi
  VARIABLES="$VARIABLES,os_flavor=$OS_FLAVOR_NAME"

  if [ -z "$OS_EXTERNAL_NET_NAME" ]
  then
    echo ===== Must specify Openstack External network name in \$OS_EXTERNAL_NET_NAME
    exit 1
  fi
  VARIABLES="$VARIABLES,os_external_net_name=$OS_EXTERNAL_NET_NAME"
fi

#### Metal
if [ "$CLOUD" == "metal" ]
then
  if [ -z "$METAL_IP_SF1" ]
  then
    echo ===== Must specify the Node 1 machine IP in \$METAL_IP_SF1
    exit 1
  fi
  VARIABLES="$VARIABLES,metal_ip_sf1=$METAL_IP_SF1"

  if [ -z "$METAL_IP_SF2" ]
  then
    echo ===== Must specify the Node 2 machine IP in \$METAL_IP_SF2
    exit 1
  fi
  VARIABLES="$VARIABLES,metal_ip_sf2=$METAL_IP_SF2"

  if [ -z "$METAL_IP_SF3" ]
  then
    echo ===== Must specify the Node 3 machine IP in \$METAL_IP_SF3
    exit 1
  fi
  VARIABLES="$VARIABLES,metal_ip_sf3=$METAL_IP_SF3"
fi

#### Localhost
if [ "$CLOUD" == "localhost" ]
then
  VARIABLES="$VARIABLES,ram_system_reservation=1.0"
  IGNORE_MTU="1"
fi

#### Shakenfist
if [ "$CLOUD" == "shakenfist" ]
then
  if [ -z "$SHAKENFIST_KEY" ]
  then
    echo ===== Must specify the Shaken Fist system key to use in \$SHAKENFIST_KEY
    exit 1
  fi
  VARIABLES="$VARIABLES,system_key=$SHAKENFIST_KEY"

  if [ -z "$SHAKENFIST_SSH_KEY" ]
  then
    echo ===== Must specify a SSH public key\'s text in \$SHAKENFIST_SSH_KEY
  fi
  VARIABLES="$VARIABLES,ssh_key=\"$SHAKENFIST_SSH_KEY\""
fi

#### Check that a valid cloud was specified
if [ -z "$CLOUD" ]
then
{
  echo ====
  echo ==== CLOUD should be specified: aws, aws-single-node, gcp, localhost, metal, openstack, shakenfist
  echo ==== eg.  ./deployandtest/sh gcp
  echo ====
  echo ==== Continuing, because you might know what you are doing...
  echo
} 2> /dev/null
fi

#### Configure system/admin key from Vault key path or specified password
if [ -n "$VAULT_SYSTEM_KEY_PATH" ]
then
  if [ -n "$ADMIN_PASSWORD" ]
  then
    echo ===== Specify either ADMIN_PASSWORD or VAULT_SYSTEM_KEY_PATH \(not both\)
    exit 1
  fi

  VARIABLES="$VARIABLES,vault_system_key_path=$VAULT_SYSTEM_KEY_PATH"
fi

set -x

#### Mode selection, deploy or hotfix at this time
if [ -z "$MODE" ]
then
  MODE="deploy"
fi

#### Default settings
BOOTDELAY="${BOOTDELAY:-2}"
ADMIN_PASSWORD="${ADMIN_PASSWORD:-Ukoh5vie}"
FLOATING_IP_BLOCK="${FLOATING_IP_BLOCK:-10.10.0.0/24}"
UNIQIFIER="${UNIQIFIER:-$USER"-"`date "+%y%m%d"`"-"`pwgen --no-capitalize -n1`"-"}"
KSM_ENABLED="${KSM_ENABLED:-1}"
GLUSTER_ENABLED="${GLUSTER_ENABLED:-0}"
GLUSTER_REPLICAS="${GLUSTER_REPLICAS:-0}"
DEPLOY_NAME="sf"
RESTORE_BACKUP="${RESTORE_BACKUP:-}"
IGNORE_MTU="${IGNORE_MTU:-0}"

# Setup variables for consumption by ansible and terraform
cwd=`pwd`

VARIABLES="$VARIABLES,cloud=$CLOUD"
VARIABLES="$VARIABLES,bootdelay=$BOOTDELAY"
VARIABLES="$VARIABLES,uniqifier=$UNIQIFIER"
VARIABLES="$VARIABLES,admin_password=$ADMIN_PASSWORD"
VARIABLES="$VARIABLES,floating_network_ipblock=$FLOATING_IP_BLOCK"
VARIABLES="$VARIABLES,mode=$MODE"
VARIABLES="$VARIABLES,ksm_enabled=$KSM_ENABLED"
VARIABLES="$VARIABLES,gluster_enabled=$GLUSTER_ENABLED"
VARIABLES="$VARIABLES,gluster_replicas=$GLUSTER_REPLICAS"
VARIABLES="$VARIABLES,deploy_name=$DEPLOY_NAME"
VARIABLES="$VARIABLES,restore_backup=\"$RESTORE_BACKUP\""
VARIABLES="$VARIABLES,ignore_mtu=\"$IGNORE_MTU\""
VARIABLES="$VARIABLES,remote_source=\"$REMOTE_SOURCE\""
VARIABLES="$VARIABLES,remote_package=\"$REMOTE_PACKAGE\""

echo "VARIABLES: $VARIABLES"
ANSIBLE_VARS=""

VARIABLES=`echo $VARIABLES | sed 's/^,//'`

OLDIFS=$IFS
IFS=,
vars=($(echo "$VARIABLES"))
IFS=$OLDIFS

mkdir -p /etc/sf/
echo "# Install started at "`date` > /etc/sf/deploy-variables
for var in "${vars[@]}"
do
  echo "#    $var" >> /etc/sf/deploy-variables
  ANSIBLE_VARS="$ANSIBLE_VARS $var"
done

encoded=`echo $ANSIBLE_VARS | base64 -w 0`

echo "" >> /etc/sf/deploy-variables
echo "export CLOUD=$CLOUD" >> /etc/sf/deploy-variables
echo "export ENCODED_ANSIBLE_VARS=$encoded" >> /etc/sf/deploy-variables
echo "export ANSIBLE_VARS=`echo $encoded | base64 -d`" >> /etc/sf/deploy-variables

ansible-playbook $VERBOSE -i hosts --extra-vars "$ANSIBLE_VARS ansible_root=\"$cwd\"" deploy.yml $@

if [ -e terraform/$CLOUD/local.yml ]
then
  ansible-playbook $VERBOSE -i hosts --extra-vars "$ANSIBLE_VARS ansible_root=\"$cwd\"" terraform/$CLOUD/local.yml $@
fi

echo "" >> /etc/sf/deploy-variables
echo "# Install finished at "`date` >> /etc/sf/deploy-variables